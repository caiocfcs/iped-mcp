"""Bind do servidor MCP via env (M-12).

Pinha os tres contratos do item: (1) defaults do baseline preservados
(127.0.0.1:8000 -- bind local continua o default seguro, D-31); (2) override
por env IPED_MCP_HOST/IPED_MCP_PORT lido via config sem tocar codigo
(monkeypatch + reload, precedente M-4); (3) porta/host invalidos viram
ValueError fail-fast nomeando a variavel de ambiente. Cobre tambem a ligacao
main.main() -> mcp.run(host=..., port=...) a partir do config.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Iterator

import pytest

import main as mcp_main
from iped_mcp import config

_SERVER_ENV = ("IPED_MCP_HOST", "IPED_MCP_PORT")


@pytest.fixture
def restore_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restaura o env do teste e recarrega o config no teardown.

    O snapshot e restaurado explicitamente dentro do proprio fixture -- nao se
    depende da ordem de finalizacao entre `monkeypatch` e este fixture
    (incidente de fixture da fase M-4).
    """
    saved = {name: os.environ.get(name) for name in _SERVER_ENV}
    yield
    for name, value in saved.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    importlib.reload(config)


def test_defaults_preservados() -> None:
    """AC1: sem env, o bind e exatamente o do baseline (loopback seguro)."""
    assert config.DEFAULT_SERVER_HOST == "127.0.0.1"
    assert config.DEFAULT_SERVER_PORT == 8000
    assert config.SERVER_HOST == "127.0.0.1"
    assert config.SERVER_PORT == 8000


def test_env_override_host_e_porta(monkeypatch: pytest.MonkeyPatch, restore_config: None) -> None:
    """AC2: env redefine o bind sem tocar codigo (reload do config)."""
    monkeypatch.setenv("IPED_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("IPED_MCP_PORT", "9000")
    importlib.reload(config)
    assert config.SERVER_HOST == "0.0.0.0"
    assert config.SERVER_PORT == 9000


def test_env_porta_aceita_bordas_validas(monkeypatch: pytest.MonkeyPatch, restore_config: None) -> None:
    """Portas 1 e 65535 sao validas (faixa TCP fechada, nao aberta)."""
    monkeypatch.setenv("IPED_MCP_PORT", "1")
    importlib.reload(config)
    assert config.SERVER_PORT == 1
    monkeypatch.setenv("IPED_MCP_PORT", "65535")
    importlib.reload(config)
    assert config.SERVER_PORT == 65535


@pytest.mark.parametrize("raw", ["abc", "0", "-1", "65536", "80.5", "", "  "])
def test_porta_invalida_erro_nomeia_env(raw: str, monkeypatch: pytest.MonkeyPatch, restore_config: None) -> None:
    """AC3: porta invalida -> ValueError na inicializacao nomeando a env."""
    monkeypatch.setenv("IPED_MCP_PORT", raw)
    with pytest.raises(ValueError, match="IPED_MCP_PORT"):
        importlib.reload(config)


def test_host_vazio_erro_nomeia_env(monkeypatch: pytest.MonkeyPatch, restore_config: None) -> None:
    """AC3: host em branco trocaria o loopback seguro por bind ambiguo -> erro."""
    monkeypatch.setenv("IPED_MCP_HOST", "   ")
    with pytest.raises(ValueError, match="IPED_MCP_HOST"):
        importlib.reload(config)


def _install_recording_mcp(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Troca o FastMCP e a sonda de startup de main() por fakes sem rede/socket.

    Args:
        monkeypatch: fixture do pytest que desfaz os patches no teardown.

    Returns:
        Dicionario que recebera os kwargs passados a ``mcp.run``.
    """
    calls: dict[str, Any] = {}

    class _RecordingMCP:
        """Substituto do FastMCP: registra os kwargs de run() sem abrir socket."""

        def run(self, **kwargs: Any) -> None:
            calls.update(kwargs)

    monkeypatch.setattr(mcp_main, "mcp", _RecordingMCP())
    monkeypatch.setattr(mcp_main, "get_router", lambda: object())
    return calls


def test_main_passa_bind_do_config(monkeypatch: pytest.MonkeyPatch, restore_config: None) -> None:
    """main() resolve host/port NO mcp.run a partir do config (nao hard-coded)."""
    calls = _install_recording_mcp(monkeypatch)
    monkeypatch.setenv("IPED_MCP_PORT", "9123")
    monkeypatch.setenv("IPED_MCP_HOST", "0.0.0.0")
    importlib.reload(config)

    mcp_main.main()

    assert calls == {"transport": "http", "host": "0.0.0.0", "port": 9123}


def test_main_usa_defaults_sem_env(monkeypatch: pytest.MonkeyPatch, restore_config: None) -> None:
    """Sem env, main() roda no bind seguro do baseline (127.0.0.1:8000)."""
    calls = _install_recording_mcp(monkeypatch)
    monkeypatch.delenv("IPED_MCP_HOST", raising=False)
    monkeypatch.delenv("IPED_MCP_PORT", raising=False)
    importlib.reload(config)

    mcp_main.main()

    assert calls == {"transport": "http", "host": "127.0.0.1", "port": 8000}
