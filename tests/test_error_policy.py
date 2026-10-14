"""Politica unica de erro HTTP (M-2 / DESIGN 02).

Camadas cobertas: client (traducao na fronteira), router (ValueError
estavel centrado em source, sem vazamento de excecoes tipadas desde a
revogacao M-14 do caso especial de search) e tool
(list_sources reporta falha como DADO). Tradugoes de transporte usadas
aqui sao injetadas via monkeypatch; os casos de status (404/500) usam o
fake da 2d real. Nenhum acesso a rede externa (AC5/AC6 do Design 01).
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from iped_mcp import errors
from iped_mcp import router as router_mod
from iped_mcp.core import tool as core_tool
from iped_mcp.router import Router
from tests.conftest import MISSING_DOC_ID, SOURCE_ID, SOURCE_ID_2, UNKNOWN_SOURCE

# Endpoint morto: porta de discard TCP (9) — conexao recusada instantanea.
DEAD_URL = "http://127.0.0.1:9"

# Prefixos/textos estaveis pinados pela AC5 (prompts de agentes calibrados).
DOC_PREFIX = "Doc não encontrado"
UNAVAILABLE_EXACT = (
    f"Source '{SOURCE_ID}' indisponível: o servidor correspondente não responde."
)
TIMEOUT_EXACT = (
    f"Timeout no source '{SOURCE_ID}': o servidor demorou demais para "
    "responder. Tente novamente ou refine a query."
)


class _BoomResponse:
    """Resposta fake suficiente para `error_for_response`/`_json`."""

    def __init__(self, status_code: int = 500, text: str = "HTTP Status 500 - boom") -> None:
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}
        self.url = "http://fake/sources/"

    def json(self) -> Any:
        raise ValueError("no json")


def _raise_for(exc: BaseException) -> Any:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise exc

    return _boom


# --- client: traducao na fronteira (AC1/AC2/AC4) --------------------------------


def test_client_timeout_becomes_iped_timeout(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """read timeout vira IpedTimeoutError com mensagem estavel (e tipo requests)."""
    monkeypatch.setattr(client._session, "request", _raise_for(requests.Timeout("read")))
    with pytest.raises(errors.IpedTimeoutError) as exc:
        client.get_document(SOURCE_ID, 5)
    assert str(exc.value) == TIMEOUT_EXACT
    assert isinstance(exc.value, requests.Timeout)  # compat p/ catchers antigos


def test_client_connection_error_becomes_offline(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """conexao recusada vira IpedConnectionError 'indisponivel' (AC2/AC5)."""
    monkeypatch.setattr(client._session, "request", _raise_for(requests.ConnectionError()))
    with pytest.raises(errors.IpedConnectionError) as exc:
        client.get_document(SOURCE_ID, 5)
    assert str(exc.value) == UNAVAILABLE_EXACT
    assert isinstance(exc.value, requests.ConnectionError)


def test_client_unreadable_2xx_becomes_response_error(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2xx sem JSON vira IpedResponseError (classe 'sem-texto')."""
    monkeypatch.setattr(client._session, "request", lambda *a, **k: _BoomResponse(200))
    with pytest.raises(errors.IpedResponseError):
        client.list_sources()


def test_client_mutation_500_is_translated(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mutacao bookmark com 500 do servidor traduzida (AC1 'mutacoes bookmark')."""
    monkeypatch.setattr(client._session, "request", lambda *a, **k: _BoomResponse(500))
    with pytest.raises(errors.ServerError) as exc:
        client.add_to_bookmark("b", [{"source": SOURCE_ID, "id": 1}])
    assert exc.value.status_code == 500
    assert "for url:" not in str(exc.value)


def test_404_and_500_are_distinct_classes(client: Any) -> None:
    """AC4: 404 e 500 chegam as camadas internas como classes distintas."""
    with pytest.raises(errors.ResourceNotFoundError) as nf:
        client.get_document(UNKNOWN_SOURCE, 1)
    with pytest.raises(errors.ServerError) as se:
        client.get_document(SOURCE_ID, MISSING_DOC_ID)
    assert type(nf.value) is not type(se.value)
    assert issubclass(errors.ResourceNotFoundError, errors.IpedHTTPError)
    assert issubclass(errors.ServerError, errors.IpedHTTPError)


# --- router: ValueError estavel centrado em source ------------------------------


def test_router_doc_500_becomes_doc_not_found_valueerror(fake_router: Any) -> None:
    """GET doc de doc inexistente: ValueError com prefixo AC5, sem texto cru."""
    with pytest.raises(ValueError) as exc:
        fake_router.get_document(SOURCE_ID, MISSING_DOC_ID)
    assert str(exc.value).startswith(DOC_PREFIX)
    assert "for url:" not in str(exc.value)


def test_router_offline_source_keeps_indisponivel_prefix(
    fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """falha de rede roteada mantem o texto exato de hoje (AC5)."""
    boom = errors.IpedConnectionError("traduzida", source_id=SOURCE_ID)
    monkeypatch.setattr(fake_router._clients["default"], "get_document", _raise_for(boom))
    with pytest.raises(ValueError) as exc:
        fake_router.get_document(SOURCE_ID, 5)
    assert str(exc.value) == UNAVAILABLE_EXACT


def test_router_timeout_source_keeps_timeout_prefix(
    fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """timeout roteado mantem o texto exato de hoje (AC5)."""
    boom = errors.IpedTimeoutError("traduzida", source_id=SOURCE_ID)
    monkeypatch.setattr(fake_router._clients["default"], "search", _raise_for(boom))
    with pytest.raises(ValueError) as exc:
        fake_router.search("*", source_id=SOURCE_ID)
    assert str(exc.value) == TIMEOUT_EXACT


def test_router_search_500_follows_standard_m2_flow(
    fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revogacao M-14: sem caso especial no router, o 500 de search vira o
    ValueError padrao M-2 com o texto traduzido do proprio erro. O nao-retry
    de 5xx em busca e garantido pela politica de classe do client (M-3),
    pinado em test_resilience.py — nao pelo vazamento da classe tipada."""
    boom = errors.ServerError("HTTP 500 traduzido", status_code=500)
    monkeypatch.setattr(fake_router._clients["default"], "search", _raise_for(boom))
    with pytest.raises(ValueError, match="HTTP 500") as exc:
        fake_router.search("*", source_id=SOURCE_ID)
    assert not isinstance(exc.value, errors.IpedError)


def test_unknown_source_when_online_is_not_found(fake_router: Any) -> None:
    """AC2: source inexistente com servidor no ar -> 'nao encontrado' (nao offline)."""
    with pytest.raises(ValueError) as exc:
        fake_router.resolve(UNKNOWN_SOURCE)
    assert str(exc.value) == (
        f"Source '{UNKNOWN_SOURCE}' não encontrado em nenhum endpoint IPED. "
        "Confira o nome com iped_list_sources."
    )


def test_unknown_source_when_all_offline_is_unavailable() -> None:
    """AC2 (corrige router.py:247-257): servidor mudo != source inexistente."""
    router = Router({"eco": DEAD_URL})
    with pytest.raises(ValueError) as exc:
        router.resolve("qualquer-coisa")
    message = str(exc.value)
    assert "Source indisponível" in message
    assert "não encontrado" not in message


# --- list_sources: falha do servidor vira DADO (AC3) ----------------------------


def test_list_sources_status_reports_failure_per_endpoint() -> None:
    """todos offline: lista vazia + motivo por endpoint (nunca [] silencioso)."""
    router = Router({"eco": DEAD_URL})
    sources, failures = router.list_sources_status()
    assert sources == []
    assert list(failures) == ["eco"]
    assert failures["eco"]


def test_list_sources_tool_reports_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """tool core.list_sources devolve sources + offline_endpoints (shape novo)."""
    router = Router({"eco": DEAD_URL})
    monkeypatch.setattr(router_mod, "_router", router)
    monkeypatch.setattr(core_tool, "_sources_cache", None)
    result = core_tool.list_sources()
    assert result["sources"] == []
    assert [e["endpoint"] for e in result["offline_endpoints"]] == ["eco"]
    assert result["offline_endpoints"][0]["error"]


def test_list_sources_tool_clean_when_online(
    fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fake respondendo: offline_endpoints vazio e sources intactos."""
    monkeypatch.setattr(core_tool, "_sources_cache", None)
    result = core_tool.list_sources()
    assert result["offline_endpoints"] == []
    assert [s["id"] for s in result["sources"]] == [SOURCE_ID, SOURCE_ID_2]
