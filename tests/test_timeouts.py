"""Timeouts por classe de operacao (DESIGN 04 / M-4).

Cobre AC2 (override por env lido do config sem tocar codigo), AC3 (doc/props
resistem ate o teto de props — asserts sobre o timeout efetivamente entregue
a Session, sem esperar 90 s reais) e AC4 (busca do core e match-all mantem
120 s). Os defaults de cada classe (AC5) tambem sao pinados aqui.
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Iterator

import pytest
import requests

from iped_mcp import config
from iped_mcp.client import IPEDClient
from tests.test_resilience import make_response

_TIMEOUT_ENV = (
    "IPED_MCP_TIMEOUT_DEFAULT",
    "IPED_MCP_TIMEOUT_SEARCH",
    "IPED_MCP_TIMEOUT_PROPS",
    "IPED_MCP_TIMEOUT_TEXT",
    "IPED_MCP_TIMEOUT_CONNECT_DOWNLOAD",
    "IPED_MCP_TIMEOUT_READ_DOWNLOAD",
)


class RecordingSession:
    """Sessao fake: registra o timeout de cada request e responde 200."""

    def __init__(self, payload: Any = None) -> None:
        self.payload = payload if payload is not None else []
        self.timeouts: list[Any] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.timeouts.append(kwargs.get("timeout"))
        resp = make_response(200, self.payload)
        # Marca o corpo como ja lido para iter_content() servir de `resp._content`
        # (sem isso requests tenta streamar resp.raw, inexistente no fake).
        resp._content_consumed = True
        return resp


@pytest.fixture
def restore_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restaura o env do teste e recarrega o config no teardown.

    O snapshot e restaurado explicitamente dentro do proprio fixture — nao se
    depende da ordem de finalizacao entre `monkeypatch` e este fixture.
    """
    saved = {name: os.environ.get(name) for name in _TIMEOUT_ENV}
    yield
    for name, value in saved.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    importlib.reload(config)


def _client_with_recorder(payload: Any = None) -> tuple[IPEDClient, RecordingSession]:
    """Cliente de producao com sessao gravadora injetada (sem rede).

    Args:
        payload: corpo `data` devolvido em todas as respostas 200.

    Returns:
        (cliente, sessao gravadora).
    """
    session = RecordingSession(payload)
    return IPEDClient("http://fake-iped", session=session), session


def test_defaults_das_classes() -> None:
    """AC5: cada classe tem o default alinhado ao icaro/design, pinado."""
    assert config.TIMEOUT_DEFAULT == 30.0
    assert config.TIMEOUT_SEARCH == 30.0
    assert config.TIMEOUT_PROPS == 90.0
    assert config.TIMEOUT_TEXT == 60.0
    assert config.TIMEOUT_CONNECT_DOWNLOAD == 10.0
    assert config.TIMEOUT_READ_DOWNLOAD == 120.0


def test_busca_usa_classe_busca() -> None:
    """GET /search recebe TIMEOUT_SEARCH (30 s default)."""
    client, session = _client_with_recorder([{"source": "s", "ids": []}])
    client.search("q", source_id="s")
    assert session.timeouts == [config.TIMEOUT_SEARCH]


def test_get_document_usa_teto_de_props() -> None:
    """AC3: /docs/{id} (get_document, caminho do ufed e do fetch de props) usa 90 s."""
    client, session = _client_with_recorder({})
    client.get_document("s", 1)
    assert session.timeouts == [config.TIMEOUT_PROPS]


def test_texto_usa_classe_texto() -> None:
    """/text recebe TIMEOUT_TEXT (60 s default)."""
    client, session = _client_with_recorder([])
    client.get_document_text("s", 1)
    assert session.timeouts == [config.TIMEOUT_TEXT]


def test_content_e_thumb_usam_par_de_download() -> None:
    """/content e /thumb recebem o par (connect, read) da classe download."""
    client, session = _client_with_recorder([])
    client.get_document_content("s", 1)
    client.get_document_thumb("s", 1)
    expected = (config.TIMEOUT_CONNECT_DOWNLOAD, config.TIMEOUT_READ_DOWNLOAD)
    assert session.timeouts == [expected, expected]


def test_operacoes_curtas_usam_default() -> None:
    """Sources e mutacoes de bookmark (sem timeout explicito) usam TIMEOUT_DEFAULT."""
    client, session = _client_with_recorder([{"id": "s", "path": "/p"}])
    client.list_sources()
    client.create_bookmark("bk")
    assert session.timeouts == [config.TIMEOUT_DEFAULT, config.TIMEOUT_DEFAULT]


def test_timeout_do_construtor_e_override_global() -> None:
    """O parametro `timeout` segue override unico de todas as classes (smoke runtime)."""
    client, session = _client_with_recorder([{"source": "s", "ids": []}])
    override = IPEDClient("http://fake-iped", timeout=7.5, session=session)
    override.search("q")
    override.get_document("s", 1)
    override.get_document_content("s", 1)
    assert session.timeouts == [7.5, 7.5, (7.5, 7.5)]
    assert client.timeout == config.TIMEOUT_DEFAULT


def test_env_override_muda_classe_efetiva_sem_tocar_codigo(
    restore_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: IPED_MCP_TIMEOUT_* no ambiente fluem para o request apos reimport."""
    monkeypatch.setenv("IPED_MCP_TIMEOUT_DEFAULT", "1")
    monkeypatch.setenv("IPED_MCP_TIMEOUT_SEARCH", "2")
    monkeypatch.setenv("IPED_MCP_TIMEOUT_PROPS", "3")
    monkeypatch.setenv("IPED_MCP_TIMEOUT_TEXT", "4")
    monkeypatch.setenv("IPED_MCP_TIMEOUT_CONNECT_DOWNLOAD", "5")
    monkeypatch.setenv("IPED_MCP_TIMEOUT_READ_DOWNLOAD", "6")
    importlib.reload(config)

    client, session = _client_with_recorder([{"source": "s", "ids": []}])
    client.list_sources()
    client.search("q")
    client.get_document("s", 1)
    client.get_document_text("s", 1)
    client.get_document_content("s", 1)
    assert session.timeouts == [1.0, 2.0, 3.0, 4.0, (5.0, 6.0)]


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_env_invalido_recusado_com_valor_error(
    restore_config: None, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Env nao numerico ou <= 0 falha no import do config, nao vira timeout infinito."""
    monkeypatch.setenv("IPED_MCP_TIMEOUT_SEARCH", raw)
    with pytest.raises(ValueError):
        importlib.reload(config)


def test_core_e_match_all_preservam_120s() -> None:
    """AC4: busca do core e match-all do scan mantem 120 s (zero regressao)."""
    from iped_mcp.core.tool import _SEARCH_TIMEOUT
    from iped_mcp.regex.scan import _MATCH_ALL_TIMEOUT

    assert _SEARCH_TIMEOUT == 120.0
    assert _MATCH_ALL_TIMEOUT == 120.0
