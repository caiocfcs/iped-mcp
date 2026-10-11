"""T-5 (DESIGN 09-B §5.6): triage_geolocation — clusters de coordenadas.

Casos pinados: happy path com tipo por categoria (a), score 50+5n cap 95 e
relevância ESTREITA >10/>3 (count 3 é BAIXA; 10 é MÉDIA) (b), Null Island
descartada e (0.0002, 0.0002) NÃO (c), bounds (d), cluster de 4 decimais com
período lexicográfico e unique_coordinate_count (e), fallback
common:geo:locations com tipo GEO_METADATA (f), probes com 0 hits/500
virando warning nominal sem exceção (g), ausência de país documentada (h),
tetos com omissão (i), contratos M-2 (source 404, eixo vazio) e orçamento
R-TRIAGE-1 com http_calls exato (j). Backend: FakeWebAPI da suíte; nenhum
HTTP real.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from iped_mcp import progress
from iped_mcp.triage import geolocation, knowledge
from tests.conftest import SOURCE_ID, UNKNOWN_SOURCE, make_doc

GPS_PROBE = 'category:"locations"'
ENVELOPE_KEYS = {
    "source_id",
    "points",
    "locations_count",
    "coordinates_by_source",
    "query_hits",
    "counters",
    "truncated",
    "warnings",
}


def _add_geo_doc(
    api: Any, doc_id: int, props: dict[str, Any], category: str = "images",
    probe: str = "locations",
) -> None:
    """Registra doc geo no fake sob uma probe de categoria do eixo.

    Args:
        api: estado observável do FakeWebAPI.
        doc_id: id do documento.
        props: propriedades geo (valores em lista, formato do jar).
        category: categoria REAL do doc (mapeia o tipo de fonte :165-174).
        probe: categoria da query ``category:"X"`` que devolve o doc.
    """
    full = dict(props)
    full["category"] = [category]
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, f"g{doc_id}", full)
    api.category_hits.setdefault(probe, []).append(doc_id)


def _gps(lat: str, lon: str, ts: str | None = None) -> dict[str, Any]:
    """Props dinâmicas GPS (chaves com latitude/longitude — gate :215-221)."""
    props: dict[str, Any] = {"GPSLatitude": [lat], "GPSLongitude": [lon]}
    if ts is not None:
        props["created"] = [ts]
    return props


def _cluster(api: Any, start: int, count: int, lat: str, lon: str) -> None:
    """N docs no MESMO ponto (cluster de evidence_count == count)."""
    for i in range(count):
        _add_geo_doc(api, start + i, _gps(lat, lon))


# --- (a) happy path -----------------------------------------------------------


def test_fluxo_feliz_exif_com_chaves_dinamicas(
    webapi: Any, fake_router: Any
) -> None:
    """Doc EXIF com GPSLatitude/GPSLongitude: ponto com tipo, período e campos."""
    _add_geo_doc(webapi, 10, _gps("-22.90000", "-43.17200", "2024-03-01T10:00:00"))

    out = geolocation.triage_geolocation(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS
    point = out["points"][0]
    assert point["source_types"] == ["EXIF"]  # categoria "images" (:165-166)
    assert point["coordinate_fields"] == ["GPSLatitude", "GPSLongitude"]
    assert point["evidence_count"] == 1
    assert (point["score"], point["relevance"]) == (55, knowledge.RELEVANCE_BAIXA)
    assert (point["first_seen"], point["last_seen"]) == (
        "2024-03-01T10:00:00", "2024-03-01T10:00:00",
    )
    assert point["justification"] == (
        "Coordenadas: -22.90000, -43.17200 | Ocorrências: 1 | "
        "Período: 2024-03-01T10:00:00 até 2024-03-01T10:00:00"
    )
    assert out["coordinates_by_source"] == {"EXIF": 1}


# --- (b) score e relevância estreita do handler (:933-934) --------------------


@pytest.mark.parametrize(
    ("count", "score", "relevance"),
    [
        (1, 55, knowledge.RELEVANCE_BAIXA),
        (3, 65, knowledge.RELEVANCE_BAIXA),  # 3 > 3 é FALSO: BAIXA (estrito)
        (4, 70, knowledge.RELEVANCE_MEDIA),
        (10, 95, knowledge.RELEVANCE_MEDIA),  # cap antes, ALTA só com >10
        (11, 95, knowledge.RELEVANCE_ALTA),
    ],
)
def test_score_e_relevance_estritos_do_handler(
    webapi: Any, fake_router: Any, count: int, score: int, relevance: str
) -> None:
    """min(50+5n,95) e limiares ESTREITOS: count 10 é MÉDIA, 11 é ALTA."""
    _cluster(webapi, 20, count, "-22.9", "-43.2")

    out = geolocation.triage_geolocation(SOURCE_ID)

    point = out["points"][0]
    assert (point["evidence_count"], point["score"], point["relevance"]) == (
        count, score, relevance,
    )


# --- (c) Null Island (:86-88 + NULL_ISLAND_EPS) --------------------------------


def test_null_island_descartada_e_vizinha_nao(
    webapi: Any, fake_router: Any
) -> None:
    """(0,0) e (5e-5,5e-5) são placeholder descartado; (2e-4,2e-4) sobrevive."""
    _add_geo_doc(webapi, 40, _gps("0.0", "0.0"))
    _add_geo_doc(webapi, 41, _gps("0.00005", "0.00005"))
    _add_geo_doc(webapi, 42, _gps("0.0002", "0.0002"))

    out = geolocation.triage_geolocation(SOURCE_ID)

    assert out["locations_count"] == 1
    point = out["points"][0]
    assert (point["latitude"], point["longitude"]) == (0.0002, 0.0002)


# --- (d) bounds (:82-83) --------------------------------------------------------


def test_coordenada_fora_de_bounds_descartada(
    webapi: Any, fake_router: Any
) -> None:
    """lat 95 não existe: doc some e o eixo fica vazio com message explícita."""
    _add_geo_doc(webapi, 50, _gps("95.0", "10.0"))

    out = geolocation.triage_geolocation(SOURCE_ID)

    assert out["points"] == []
    assert out["locations_count"] == 0
    assert out["message"] == knowledge.GEO_EMPTY_MESSAGE


# --- (e) cluster de 4 decimais + período lexicográfico --------------------------


def test_cluster_11m_periodo_e_unicas(webapi: Any, fake_router: Any) -> None:
    """~11 m: -22.90004 e -22.90001 agrupam; período lexicográfico ordena;
    coordenadas cruas únicas contam a poluição (:90-94/:135-149)."""
    _add_geo_doc(webapi, 60, _gps("-22.90004", "-43.17200", "2024-05-05"))
    _add_geo_doc(webapi, 61, _gps("-22.90001", "-43.17200", "2023-01-01"))
    _add_geo_doc(webapi, 62, _gps("-22.90004", "-43.17200", "2024-01-01"))

    out = geolocation.triage_geolocation(SOURCE_ID)

    point = out["points"][0]
    assert (point["latitude"], point["longitude"]) == (-22.9, -43.172)
    assert point["evidence_count"] == 3
    assert point["unique_coordinate_count"] == 2  # doc 62 duplica o 60
    assert (point["first_seen"], point["last_seen"]) == ("2023-01-01", "2024-05-05")


def test_timestamp_ausente_vira_NI(webapi: Any, fake_router: Any) -> None:
    """Sem timestamp: período N/I dos dois lados (:147/:149)."""
    _add_geo_doc(webapi, 70, _gps("-22.9", "-43.2"))

    point = geolocation.triage_geolocation(SOURCE_ID)["points"][0]

    assert (point["first_seen"], point["last_seen"]) == ("N/I", "N/I")


# --- (f) fallback common:geo:locations -----------------------------------------


def test_fallback_geo_locations_tipo_metadata(webapi: Any, fake_router: Any) -> None:
    """Sem chaves lat/lon: regex no campo geo do jar; tipo GEO_METADATA (:174)."""
    _add_geo_doc(
        webapi, 80, {"common:geo:locations": ["-15.78,-47.93"]},
        category="documents",
    )

    out = geolocation.triage_geolocation(SOURCE_ID)

    point = out["points"][0]
    assert point["source_types"] == ["GEO_METADATA"]
    assert point["coordinate_fields"] == ["common:geo:locations"]
    assert (point["latitude"], point["longitude"]) == (-15.78, -47.93)


def test_categoria_mapeada_chat_e_a_crua_senao(webapi: Any, fake_router: Any) -> None:
    """CHAT por substring (:167-168); categoria fora do mapeamento → crua."""
    _add_geo_doc(webapi, 85, _gps("-10.1", "-50.1"), category="chat export")
    _add_geo_doc(webapi, 86, _gps("-10.2", "-50.2"), category="relatorio-x")

    types = sorted(
        t for p in geolocation.triage_geolocation(SOURCE_ID)["points"]
        for t in p["source_types"]
    )

    assert types == ["CHAT", "relatorio-x"]


# --- (g) probes: 0 hits e 500 viram warning, nunca exceção (§5.1, M-7) ---------


def test_probe_zero_hit_e_falha_agem_com_warning_nominal(
    webapi: Any, fake_router: Any
) -> None:
    """Probe 500: hits -1 + warning nominal; sonda de campo sem hits reporta a
    query literal; as demais seguem e o eixo produz resultado."""
    webapi.fail_search_queries.add(GPS_PROBE)
    _add_geo_doc(webapi, 90, _gps("-22.9", "-43.2"), probe="gps")

    out = geolocation.triage_geolocation(SOURCE_ID)

    assert out["query_hits"][GPS_PROBE] == -1
    assert any(f"probe {GPS_PROBE} falhou" in w for w in out["warnings"])
    assert any(
        knowledge.GEO_PROBE_QUERIES[0] in w for w in out["warnings"]
    )  # a sonda de campo reporta o 0-hit com a query literal
    assert [p["evidence_count"] for p in out["points"]] == [1]


# --- (h) sem país (CoordinatesToCountryTool é ICARO-side, §5.3) ----------------


def test_shape_nao_contem_pais_estado_cidade(
    webapi: Any, fake_router: Any
) -> None:
    """Ausente ≠ falso: country/state/city NUNCA aparecem no shape."""
    _add_geo_doc(webapi, 95, _gps("-22.9", "-43.2"))

    point = geolocation.triage_geolocation(SOURCE_ID)["points"][0]

    assert all(key not in point for key in ("country", "state", "city"))


# --- (i) teto de pontos com omissão contada (§5.4) ------------------------------


def test_teto_de_100_pontos_marcado_com_omissao(
    webapi: Any, fake_router: Any
) -> None:
    """102 clusters: entrega 100, locations_count 102, truncated com 2."""
    for i in range(knowledge.GEO_POINTS_TOP + 2):
        _add_geo_doc(webapi, 200 + i, _gps(f"-20.{i:04d}", f"-40.{i:04d}"))

    out = geolocation.triage_geolocation(SOURCE_ID)

    assert len(out["points"]) == knowledge.GEO_POINTS_TOP
    assert out["locations_count"] == 102
    assert out["truncated"]["points_truncated"] is True
    assert out["truncated"]["points_omitted"] == 2


# --- (j) contratos M-2 e orçamento ----------------------------------------------


def test_source_inexistente_vira_value_error_estavel(fake_router: Any) -> None:
    """404 de source: ValueError estável ANTES de qualquer HTTP (M-2)."""
    with pytest.raises(ValueError, match="não encontrado em nenhum endpoint"):
        geolocation.triage_geolocation(UNKNOWN_SOURCE)


def test_eixo_vazio_e_mensagem_explicita_sem_excecao(
    webapi: Any, fake_router: Any
) -> None:
    """Nada geo: message explícita + as 6 probes pagas; sem exceção (§0.1)."""
    out = geolocation.triage_geolocation(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS | {"message"}
    assert out["message"] == knowledge.GEO_EMPTY_MESSAGE
    assert out["counters"]["http_calls"] == len(knowledge.GEO_PROBE_QUERIES)


def test_orcamento_estourado_vira_parcial_marcado(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap 7 = 6 probes + 1 doc: parcial não vazio, nunca exceção (§2.3)."""
    monkeypatch.setitem(knowledge.TOOL_BUDGETS, "triage_geolocation", 7)
    _cluster(webapi, 300, 2, "-22.9", "-43.2")

    out = geolocation.triage_geolocation(SOURCE_ID)

    counters, truncated = out["counters"], out["truncated"]
    assert (counters["phase"], counters["http_calls"]) == (
        progress.PHASE_PARTIAL, 7,
    )
    assert truncated["http_budget_exceeded"] is True
    assert truncated["docs_omitted_by_budget"] == 1
    assert any("http_budget_exceeded" in w and "cap 7" in w for w in out["warnings"])
    assert out["locations_count"] == 1  # parcial NÃO vazio


def test_http_calls_exato_e_igual_ao_trafego_do_fake(
    webapi: Any, fake_router: Any
) -> None:
    """Pino de exatidão: 6 probes + N docs == http_calls == fake."""
    _cluster(webapi, 320, 2, "-22.9", "-43.2")
    webapi.requests_log.clear()

    out = geolocation.triage_geolocation(SOURCE_ID)

    paths = [entry["path"] for entry in webapi.requests_log]
    searches = paths.count("/search")
    docs = sum("/docs/" in p for p in paths)
    assert searches == len(knowledge.GEO_PROBE_QUERIES) == 6
    assert out["counters"]["http_calls"] == searches + docs == 6 + 2
    assert out["truncated"]["http_budget_exceeded"] is False


# --- registro MCP ---------------------------------------------------------------


def test_tool_registrada_via_mcp(fake_router: Any, webapi: Any) -> None:
    """triage_geolocation é descoberta e chamada pelo MCP (mesmo mecanismo)."""
    from fastmcp import Client, FastMCP

    from iped_mcp.triage import register as register_triage

    server = FastMCP("iped_mcp_test")
    register_triage(server)

    async def _run() -> dict[str, Any]:
        async with Client(server) as client:
            names = [t.name for t in await client.list_tools()]
            assert "triage_geolocation" in names
            result = await client.call_tool(
                "triage_geolocation", {"source_id": SOURCE_ID}
            )
            return (
                result.data
                if result.data is not None
                else json.loads(result.content[0].text)
            )

    payload = asyncio.run(_run())
    assert payload["source_id"] == SOURCE_ID
    assert payload["message"] == knowledge.GEO_EMPTY_MESSAGE
