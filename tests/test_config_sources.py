"""M-17: fonte unica de configuracao (dotenv + default de endereco WebAPI).

Pins da fase:

- `load_dotenv()` executa UMA unica vez, no `iped_mcp.config` (o client nao
  carrega .env por conta propria);
- o default historico `http://localhost:1234` segue valido byte-a-byte, agora
  em `config.DEFAULT_IPED_URL`, e `config.IPED_URL` obedece ao env `IPED_URL`;
- `IPEDClient()` sem `base_url` usa `config.IPED_URL` (resolvido na
  construcao); `base_url` explicito continua vencendo;
- nenhum modulo de tool redefines `EXPORTS_DIR` (fonte unica do M-10/M-17).
"""

from __future__ import annotations

import importlib

import pytest

from iped_mcp import client as client_module
from iped_mcp import config
from iped_mcp.client import IPEDClient
from iped_mcp.regex import tool as regex_tool
from iped_mcp.whatsapp import tool as whatsapp_tool

# Default do baseline (client.py pre-M-17); pin byte-a-byte.
BASELINE_IPED_URL = "http://localhost:1234"


def test_default_iped_url_preserva_baseline() -> None:
    """O default do baseline continua `http://localhost:1234` (byte-a-byte)."""
    assert config.DEFAULT_IPED_URL == BASELINE_IPED_URL


def test_env_iped_url_alimenta_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """`IPED_URL` no env define `config.IPED_URL` (leitura unica no config)."""
    monkeypatch.setenv("IPED_URL", "http://webapi-casa:8080")
    importlib.reload(config)
    try:
        assert config.IPED_URL == "http://webapi-casa:8080"
    finally:
        monkeypatch.delenv("IPED_URL", raising=False)
        importlib.reload(config)
    assert config.IPED_URL == BASELINE_IPED_URL


def test_client_default_base_url_vem_do_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`IPEDClient()` sem base_url resolve `config.IPED_URL` na construcao."""
    monkeypatch.setattr(config, "IPED_URL", "http://from-config:9999")
    assert IPEDClient().base_url == "http://from-config:9999"


def test_client_base_url_explicito_vence() -> None:
    """base_url explicito no construtor nunca e sobreposto pelo config."""
    assert IPEDClient("http://explicita:1234/").base_url == "http://explicita:1234"


def test_client_nao_carrega_dotenv_nem_redefine_default() -> None:
    """client.py nao importa dotenv nem retem default proprio de URL (M-17)."""
    assert not hasattr(client_module, "load_dotenv")
    assert not hasattr(client_module, "DEFAULT_IPED_URL")


def test_load_dotenv_executa_so_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importar config carrega o .env 1x; importar o client nao carrega de novo."""
    calls: list[str] = []
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: calls.append("call"))
    try:
        importlib.reload(config)
        importlib.reload(client_module)
    finally:
        monkeypatch.undo()
        importlib.reload(config)
        importlib.reload(client_module)
    assert calls == ["call"]


@pytest.mark.parametrize(
    "module", [regex_tool, whatsapp_tool], ids=["regex", "whatsapp"]
)
def test_tools_nao_redefinem_exports_dir(module: object) -> None:
    """Nao resta constante propria de exports dir nas tools (fonte: config)."""
    assert not hasattr(module, "EXPORTS_DIR")


def test_config_e_a_fonte_do_exports_dir() -> None:
    """`config.EXPORTS_DIR` e o unico ponto do default de exports (M-10/M-17)."""
    assert hasattr(config, "EXPORTS_DIR")
    assert hasattr(config, "DEFAULT_EXPORTS_DIR")
