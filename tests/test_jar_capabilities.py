"""M-11: detecção de capacidade do jar no startup (feature detection).

Pinos da fase:

- jar sem recursos (fixtures 2d) → todas as capacidades False e NENHUM
  parâmetro de fork no tráfego das tools (AC1/AC3, NR-3/R9);
- jar fake COM os recursos (fixtures do fork: P1-1/F-5/F-1) → capacidades
  True, inclusive o caso-armadilha "200 sem efeito" do `fields=` (P2-8),
  que NUNCA pode virar detecção;
- sonda que falha → capacidades desligadas, startup intacto (AC2);
- `Router.supports` só liga com env IPED_MCP_FORK_FEATURES=1 E detecção;
- iped_list_sources expõe as capacidades como DADO (o agente vê com que
  jar fala).
"""

from __future__ import annotations

import dataclasses
import importlib
from typing import Any

import pytest

from iped_mcp import config
from iped_mcp import router as router_mod
from iped_mcp.core import tool as core_tool
from iped_mcp.router import JarCapabilities, Router
from tests.conftest import SOURCE_ID, make_doc

DEAD_URL = "http://127.0.0.1:9"
_ALL_FALSE = JarCapabilities()
_CAP_KEYS = ("search_pagination", "case_summary", "properties_fields")


def _set_jar(
    webapi: Any,
    *,
    pagination: bool = False,
    case: bool = False,
    fields: str = "absent",
) -> Any:
    """Configura o fake como jar fork/plain ANTES de construir o Router."""
    webapi.has_pagination = pagination
    webapi.has_case_summary = case
    webapi.has_properties_fields = fields
    return webapi


def _router_for(webapi: Any) -> Router:
    return Router({"default": webapi.base_url})


def _probe_searches(api: Any) -> list[dict[str, Any]]:
    """Entradas de /search do log cujo q é um dos dois probes do startup."""
    return [
        e
        for e in api.requests_log
        if e["path"] == "/search"
        and e["query"].get("q", [""])[0]
        in {router_mod._PROBE_TERM, "*"}
        and "limit" in e["query"]
    ]


# --- estrutura imutável ------------------------------------------------------


def test_capabilities_sao_imutaveis_e_json_seguras() -> None:
    caps = JarCapabilities(search_pagination=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.case_summary = True  # type: ignore[misc]
    assert caps.as_dict() == {
        "search_pagination": True,
        "case_summary": False,
        "properties_fields": False,
    }


# --- matriz de detecção (AC2) -------------------------------------------------


def test_jar_plain_todas_capacidades_false(webapi: Any) -> None:
    caps = _router_for(webapi).capabilities()
    assert caps["default"] == _ALL_FALSE
    assert not any(caps["default"].as_dict().values())


def test_fork_detecta_as_tres_capacidades(webapi: Any) -> None:
    _set_jar(webapi, pagination=True, case=True, fields="filtered")
    caps = _router_for(webapi).capabilities()["default"]
    assert (caps.search_pagination, caps.case_summary) == (True, True)
    assert caps.properties_fields is True


def test_so_paginacao_detectada(webapi: Any) -> None:
    _set_jar(webapi, pagination=True)
    caps = _router_for(webapi).capabilities()["default"]
    assert caps.search_pagination is True
    assert caps.case_summary is False
    assert caps.properties_fields is False


def test_fields_sem_paginacao_usa_doc_fallback(webapi: Any) -> None:
    """Sem limit funcional a sonda tenta o doc-id de fallback (3 chamadas)."""
    webapi.docs[(SOURCE_ID, 1)] = make_doc(1, "probe.txt", {"hash": ["aa"]})
    _set_jar(webapi, fields="filtered")
    caps = _router_for(webapi).capabilities()["default"]
    assert caps.search_pagination is False
    assert caps.properties_fields is True


def test_fields_200_sem_efeito_nao_e_deteccao(webapi: Any) -> None:
    """P2-8: 200 com propriedades COMPLETAS = parâmetro ignorado = capacidade
    ausente — o falso positivo que o Design 11 exige rejeitar."""
    webapi.docs[(SOURCE_ID, 1)] = make_doc(1, "probe.txt", {"hash": ["aa"]})
    _set_jar(webapi, fields="noeffect")
    caps = _router_for(webapi).capabilities()["default"]
    assert caps.properties_fields is False
    probe = [e for e in webapi.requests_log if e["path"].endswith("/properties")]
    assert probe and probe[0]["status"] == 200  # respondeu, e ainda assim False


def test_sonda_paginacao_usa_termo_inexistente_e_limit_1(webapi: Any) -> None:
    """Probe com q inexistente (resposta mínima) e limit=1 explícito."""
    _set_jar(webapi, pagination=True)
    _router_for(webapi)
    hits = [
        e
        for e in webapi.requests_log
        if e["path"] == "/search"
        and e["query"].get("q") == [router_mod._PROBE_TERM]
    ]
    assert len(hits) == 1
    assert hits[0]["query"].get("limit") == ["1"]
    assert hits[0]["query"].get("sourceID") == [SOURCE_ID]


def test_sonda_inesperada_nao_crasha_o_startup(
    webapi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exceção não-Iped num probe: capacidades desligadas, servidor de pé."""

    def boom(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("sonda explodiu")

    monkeypatch.setattr(router_mod, "_probe_pagination", boom)
    router = _router_for(webapi)  # não pode lançar
    assert router.capabilities()["default"] == _ALL_FALSE


def test_endpoint_offline_nao_e_sondado() -> None:
    """Endpoint mudo já é reportado por list_sources_status; sem sonda."""
    router = Router({"eco": DEAD_URL})
    assert router.capabilities() == {}


# --- gate env × detecção (AC3/NR-3) -------------------------------------------


def test_supports_exige_env_ligado_e_deteccao(
    webapi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_jar(webapi, pagination=True, case=True, fields="filtered")
    router = _router_for(webapi)
    monkeypatch.setattr(config, "FORK_FEATURES_ENABLED", False)
    assert not any(
        router.supports(cap, SOURCE_ID) for cap in _CAP_KEYS
    ), "env desligado: nada pode estar ativo (R9)"
    monkeypatch.setattr(config, "FORK_FEATURES_ENABLED", True)
    assert all(router.supports(cap, SOURCE_ID) for cap in _CAP_KEYS)


def test_supports_jar_plain_false_mesmo_com_env_ligado(
    webapi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _router_for(webapi)
    monkeypatch.setattr(config, "FORK_FEATURES_ENABLED", True)
    assert not any(router.supports(cap, SOURCE_ID) for cap in _CAP_KEYS)


def test_supports_nome_invalido_nomeia_a_capacidade(
    webapi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _router_for(webapi)
    monkeypatch.setattr(config, "FORK_FEATURES_ENABLED", True)
    with pytest.raises(ValueError, match="telepatia"):
        router.supports("telepatia", SOURCE_ID)
    assert router.supports("case_summary", "source-inexistente") is False


# --- exposição na tool list_sources (dado, não exceção) ------------------------


def test_list_sources_expoe_capacidades_do_fork(
    webapi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_jar(webapi, pagination=True, case=True, fields="filtered")
    monkeypatch.setattr(router_mod, "_router", _router_for(webapi))
    monkeypatch.setattr(core_tool, "_sources_cache", None)
    monkeypatch.setattr(config, "FORK_FEATURES_ENABLED", True)
    result = core_tool.list_sources()
    payload = result["jar_capabilities"]
    assert payload["fork_features_enabled"] is True
    entry = payload["endpoints"][0]
    assert entry["endpoint"] == "default"
    assert all(entry[cap] is True for cap in _CAP_KEYS)


def test_list_sources_jar_plain_reporta_tudo_false(
    fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_tool, "_sources_cache", None)
    result = core_tool.list_sources()
    payload = result["jar_capabilities"]
    assert payload["fork_features_enabled"] is False
    entry = payload["endpoints"][0]
    assert all(entry[cap] is False for cap in _CAP_KEYS)


# --- AC1/AC3: tráfego das tools nunca leva parâmetro de fork -------------------


def test_tools_jamais_enviam_parametros_de_fork(
    webapi: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Até com jar fork E env ligado: as tools atuais não têm superfície de
    fork — nenhuma requisição de tool leva limit/offset/fields (NR-3/R9). Os
    únicos /search com limit no log são as duas sondas do startup."""
    _set_jar(webapi, pagination=True, case=True, fields="filtered")
    monkeypatch.setattr(router_mod, "_router", _router_for(webapi))
    monkeypatch.setattr(core_tool, "_sources_cache", None)
    monkeypatch.setattr(config, "FORK_FEATURES_ENABLED", True)
    core_tool.list_sources()
    core_tool.search("*", SOURCE_ID)
    webapi.requests_log.clear()  # a partir daqui só tráfego de tool
    core_tool._sources_cache = None
    core_tool.list_sources()
    core_tool.search("*", SOURCE_ID)
    for entry in webapi.requests_log:
        query = entry["query"]
        assert "limit" not in query, entry["raw_target"]
        assert "offset" not in query, entry["raw_target"]
        assert "fields" not in query, entry["raw_target"]
        assert not entry["path"].endswith("/properties"), entry["raw_target"]


# --- config: env IPED_MCP_FORK_FEATURES ----------------------------------------


def test_fork_features_default_desligado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IPED_MCP_FORK_FEATURES", raising=False)
    importlib.reload(config)
    try:
        assert config.DEFAULT_FORK_FEATURES == "0"
        assert config.FORK_FEATURES_ENABLED is False
    finally:
        importlib.reload(config)


@pytest.mark.parametrize("raw", ["1", " 1 "])
def test_fork_features_env_1_liga(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("IPED_MCP_FORK_FEATURES", raw)
    importlib.reload(config)
    try:
        assert config.FORK_FEATURES_ENABLED is True
    finally:
        monkeypatch.delenv("IPED_MCP_FORK_FEATURES", raising=False)
        importlib.reload(config)


@pytest.mark.parametrize("raw", ["0", "", "true", "2"])
def test_fork_features_qualquer_outro_valor_deixa_off(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("IPED_MCP_FORK_FEATURES", raw)
    importlib.reload(config)
    try:
        assert config.FORK_FEATURES_ENABLED is False
    finally:
        monkeypatch.delenv("IPED_MCP_FORK_FEATURES", raising=False)
        importlib.reload(config)
