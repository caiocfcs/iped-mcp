"""T-7 (DESIGN 09-B §7.8): triage_devices_networks — só agregados.

Casos pinados: parse dos 3 formatos de ``ufed:Application`` + fallback
(:106-144), colapso IPv6 por /64, IP inválido descartado, fingerprint
dedup acumulando apps, ``services_summary`` com rótulo/fallback e orden por
hits, wifi dedup preservando ordem (categoria OU ``ufed:Source``),
bluetooth dedup, tetos de seção marcados ([:50]/[:100]), vazio com mensagem
(None do ICARO :280-281 → §0.1), 500/404, orçamento R-TRIAGE-1 e pin
NEGATIVO: o envelope NÃO contém a timeline bruta (``connections``).
Backend: FakeWebAPI; nenhum HTTP real.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from iped_mcp import progress
from iped_mcp.triage import devices_networks, knowledge, tool
from tests.conftest import SOURCE_ID, UNKNOWN_SOURCE, make_doc

ENVELOPE_KEYS = {
    "source_id",
    "connections_by_ip",
    "services_summary",
    "device_fingerprints",
    "wifi_networks",
    "bluetooth_devices",
    "connections_total",
    "logs_parsed",
    "logs_total",
    "query_hits",
    "counters",
    "truncated",
    "warnings",
}
TS_MORNING = "14/08/2026 09:00:00 UTC"
TS_AFTERNOON = "14/08/2026 17:25:13 UTC"


def _add_log_doc(
    api: Any,
    doc_id: int,
    body: str,
    app: str = "",
    ts: str = TS_AFTERNOON,
) -> None:
    """LogEntry no fake: ``ufed:Body``/``ufed:Application``/``ufed:TimeStamp``."""
    props: dict[str, Any] = {"ufed:Body": [body], "category": ["logs"]}
    if app:
        props["ufed:Application"] = [app]
    if ts:
        props["ufed:TimeStamp"] = [ts]
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, "logentry", props)
    api.category_hits.setdefault("logs", []).append(doc_id)


def _add_prop_doc(
    api: Any, doc_id: int, category: str, props: dict[str, Any]
) -> None:
    """Doc de wifi/bluetooth no fake sob a categoria da query dada."""
    full = dict(props)
    full["category"] = [category]
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, "entry", full)
    api.category_hits.setdefault(category, []).append(doc_id)


# --- (a) happy path + pin negativo da timeline bruta ------------------------------


def test_fluxo_feliz_agregados_e_sem_timeline_bruta(
    webapi: Any, fake_router: Any
) -> None:
    """2 logs do mesmo IP: agregados completos e ``connections`` ausente."""
    _add_log_doc(webapi, 500, "Service=CLOUDPHOTOLIBRARY IP=200.10.0.1",
                 app="23F77; iPhone15,2")
    _add_log_doc(webapi, 501, "Service=FMIP IP=200.10.0.1", ts=TS_MORNING)

    out = tool.triage_devices_networks(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS
    assert "connections" not in out  # pin NEGATIVO §7.8: nunca a timeline
    slot = out["connections_by_ip"][0]
    assert slot["ip"] == "200.10.0.1"
    assert slot["hits"] == 2
    assert slot["services"] == ["CLOUDPHOTOLIBRARY", "FMIP"]  # sorted :217
    assert slot["first_seen"] == "2026-08-14T09:00:00"
    assert slot["last_seen"] == "2026-08-14T17:25:13"
    assert [s["label"] for s in out["services_summary"]] == [
        "Buscar (Find My)", "Fotos do iCloud",
    ]
    assert (out["connections_total"], out["logs_parsed"], out["logs_total"]) == (2, 2, 2)


# --- (b) formatos de ufed:Application (:106-144) -----------------------------------


@pytest.mark.parametrize(
    ("app", "expected"),
    [
        ("<D73AP> <iPhone OS;26.6;23G71> <fmflocatord/7.0>",
         {"hw_model": "D73AP", "build": "23G71", "os_version": "26.6",
          "model": "", "app": "fmflocatord"}),                      # Formato B
        ("fmflocatord/7.0 iPhone15,2/23F77",
         {"hw_model": "", "build": "23F77", "os_version": "",
          "model": "iPhone15,2", "app": "fmflocatord/7.0"}),         # Formato C
        ("23F77; iPhone15,2",
         {"hw_model": "", "build": "23F77", "os_version": "",
          "model": "iPhone15,2", "app": ""}),                        # Formato A
        ("qualquer coisa iPhone9,1 aqui",
         {"hw_model": "", "build": "", "os_version": "",
          "model": "iPhone9,1", "app": ""}),                         # fallback
    ],
)
def test_formatos_application(app: str, expected: dict) -> None:
    """Os 4 caminhos de ``_parse_application`` são literais do normalizer."""
    assert devices_networks._parse_application(app) == expected


# --- (c) IPs: colapso /64 e descarte ------------------------------------------------


def test_ipv6_colapsa_no_prefixo_64(webapi: Any, fake_router: Any) -> None:
    """2 IPs v6 do mesmo /64 viram UMA entrada com ip = prefixo (:199-215)."""
    _add_log_doc(webapi, 510, "Service=FMIP IP=2804:14d:5000:c2aa::1")
    _add_log_doc(webapi, 511, "Service=FMF IP=2804:14d:5000:c2aa:1234::5")

    out = tool.triage_devices_networks(SOURCE_ID)

    assert len(out["connections_by_ip"]) == 1
    slot = out["connections_by_ip"][0]
    assert slot["ip"] == "2804:14d:5000:c2aa::/64"
    assert slot["ip_version"] == 6
    assert slot["hits"] == 2


def test_ip_invalido_descartado_contado(webapi: Any, fake_router: Any) -> None:
    """IP inválido: log contado mas não parseado (:166-168); eixo vazio → message."""
    _add_log_doc(webapi, 512, "Service=FMIP IP=999.1.1.1")

    out = tool.triage_devices_networks(SOURCE_ID)

    assert (out["logs_total"], out["logs_parsed"]) == (1, 0)
    assert out["connections_by_ip"] == []
    assert out["message"] == knowledge.DN_EMPTY_MESSAGE


def test_corpo_fora_do_padrao_ignorado(webapi: Any, fake_router: Any) -> None:
    """Body sem ``Service=… IP=…`` não vira conexão (parse_log_entry None)."""
    _add_log_doc(webapi, 513, "backup iniciado sem campos de serviço")

    out = tool.triage_devices_networks(SOURCE_ID)

    assert (out["logs_total"], out["logs_parsed"]) == (1, 0)


# --- (d) fingerprints e serviços ------------------------------------------------------


def test_fingerprint_dedup_acumula_apps(webapi: Any, fake_router: Any) -> None:
    """Dedup por (model, hw, build): apps_observed sorted e hits (:232-251)."""
    _add_log_doc(webapi, 520, "Service=FMIP IP=10.0.0.1",
                 app="<D73AP> <iPhone OS;26.6;23G71> <fmflocatord/7.0>")
    _add_log_doc(webapi, 521, "Service=FMF IP=10.0.0.2",
                 app="<D73AP> <iPhone OS;26.6;23G71> <cloudd/720.1>")

    out = tool.triage_devices_networks(SOURCE_ID)

    assert len(out["device_fingerprints"]) == 1
    fp = out["device_fingerprints"][0]
    assert fp["hw_model"] == "D73AP"
    assert fp["os_version"] == "26.6"
    assert fp["apps_observed"] == ["cloudd", "fmflocatord"]
    assert fp["hits"] == 2


def test_services_summary_hits_desc_e_fallback_cru(webapi: Any, fake_router: Any) -> None:
    """Rótulo legível quando conhecido, código cru senão; ordem por hits (:220-230)."""
    _add_log_doc(webapi, 530, "Service=CLOUDPHOTOLIBRARY IP=10.0.0.1")
    _add_log_doc(webapi, 531, "Service=CLOUDPHOTOLIBRARY IP=10.0.0.2")
    _add_log_doc(webapi, 532, "Service=XYZSRV IP=10.0.0.3")

    out = tool.triage_devices_networks(SOURCE_ID)

    assert [(s["service"], s["label"], s["hits"]) for s in out["services_summary"]] == [
        ("CLOUDPHOTOLIBRARY", "Fotos do iCloud", 2),
        ("XYZSRV", "XYZSRV", 1),
    ]


# --- (e) wifi e bluetooth ---------------------------------------------------------------


def test_wifi_dedup_preserva_ordem_e_source_marker(
    webapi: Any, fake_router: Any
) -> None:
    """SSID dedup na ordem (:79-81); props sem categoria-wireless entram pelo
    ``ufed:Source`` com ``wifi`` (:77) — bucket por props, não por query."""
    for offset, ssid in enumerate(["WiFi-Z", "WiFi-A", "WiFi-Z", "  ", "WiFi-B"]):
        _add_prop_doc(webapi, 540 + offset, "wireless networks",
                      {"ufed:SSId": [ssid]})
    _add_prop_doc(webapi, 560, "wireless networks",
                  {"ufed:SSID": ["WiFi-Source"], "ufed:Source": ["Native WiFi"]})

    out = tool.triage_devices_networks(SOURCE_ID)

    assert out["wifi_networks"] == ["WiFi-Z", "WiFi-A", "WiFi-B", "WiFi-Source"]


def test_bluetooth_dedup_exato(webapi: Any, fake_router: Any) -> None:
    """Nomes bluetooth dedup stripados na ordem (:84-88)."""
    for offset, name in enumerate(["Headphone X", "Headphone X", "Car Kit"]):
        _add_prop_doc(webapi, 570 + offset, "bluetooth devices",
                      {"ufed:DeviceName": [name]})

    out = tool.triage_devices_networks(SOURCE_ID)

    assert out["bluetooth_devices"] == ["Headphone X", "Car Kit"]


# --- (f) tetos de seção marcados ----------------------------------------------------------


def test_teto_wifi_100_marcado(webapi: Any, fake_router: Any) -> None:
    """102 SSIDs: entrega 100 e marca omissão (:278 + §7.6)."""
    for i in range(102):
        _add_prop_doc(webapi, 600 + i, "wireless networks",
                      {"ufed:SSId": [f"NET-{i:03d}"]})

    out = tool.triage_devices_networks(SOURCE_ID)

    assert len(out["wifi_networks"]) == knowledge.DN_WIFI_NETWORKS_MAX
    assert out["truncated"]["wifi_truncated"] is True
    assert out["truncated"]["wifi_omitted"] == 2


def test_teto_by_ip_50_por_hits(webapi: Any, fake_router: Any) -> None:
    """52 IPs (um com 2 hits): visão [:50], líder por hits, omissão marcada."""
    for i in range(52):
        _add_log_doc(webapi, 700 + i, f"Service=FMIP IP=10.0.0.{i}")
    _add_log_doc(webapi, 800, "Service=FMF IP=10.0.0.0")

    out = tool.triage_devices_networks(SOURCE_ID)

    assert len(out["connections_by_ip"]) == knowledge.DN_CONNECTIONS_BY_IP_MAX
    assert out["connections_by_ip"][0]["ip"] == "10.0.0.0"
    assert out["connections_by_ip"][0]["hits"] == 2
    assert out["truncated"]["connections_truncated"] is True
    assert out["truncated"]["connections_by_ip_omitted"] == 2
    assert out["connections_total"] == 53  # total completo mesmo com visão cortada


# --- (g) contratos M-2, orçamento e exatidão -----------------------------------------------


def test_query_500_vira_hit_negativo_e_warning(webapi: Any, fake_router: Any) -> None:
    """500 em category:\"logs\": -1 + warning; wifi segue normal."""
    webapi.fail_search_queries.add('category:"logs"')
    _add_prop_doc(webapi, 850, "wireless networks", {"ufed:SSId": ["NET-X"]})

    out = tool.triage_devices_networks(SOURCE_ID)

    assert out["query_hits"]['category:"logs"'] == -1
    assert any('category:"logs"' in w and "falhou" in w for w in out["warnings"])
    assert out["wifi_networks"] == ["NET-X"]


def test_source_inexistente_vira_value_error_estavel(fake_router: Any) -> None:
    """404 de source: ValueError estável ANTES de qualquer HTTP (M-2)."""
    with pytest.raises(ValueError, match="não encontrado em nenhum endpoint"):
        tool.triage_devices_networks(UNKNOWN_SOURCE)


def test_eixo_vazio_e_mensagem_explicita(webapi: Any, fake_router: Any) -> None:
    """Nada aproveitável: message (None do ICARO → §0.1) + as 4 queries pagas."""
    out = tool.triage_devices_networks(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS | {"message"}
    assert out["message"] == knowledge.DN_EMPTY_MESSAGE
    assert out["counters"]["http_calls"] == len(devices_networks._dn_queries()) == 4


def test_orcamento_estourado_vira_parcial_marcado(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap 5 = 4 queries + 1 doc: parcial não vazio, nunca exceção (§2.3)."""
    monkeypatch.setitem(knowledge.TOOL_BUDGETS, "triage_devices_networks", 5)
    for i in range(3):
        _add_log_doc(webapi, 880 + i, f"Service=FMIP IP=10.9.{i}.1")

    out = tool.triage_devices_networks(SOURCE_ID)

    counters, truncated = out["counters"], out["truncated"]
    assert (counters["phase"], counters["http_calls"]) == (
        progress.PHASE_PARTIAL, 5,
    )
    assert truncated["http_budget_exceeded"] is True
    assert truncated["docs_omitted_by_budget"] == 2
    assert any("http_budget_exceeded" in w and "cap 5" in w for w in out["warnings"])
    assert out["connections_total"] == 1  # parcial NÃO vazio


def test_http_calls_exato_e_igual_ao_trafego_do_fake(
    webapi: Any, fake_router: Any
) -> None:
    """Pino de exatidão: 4 queries + N docs == http_calls == fake."""
    for i in range(3):
        _add_log_doc(webapi, 890 + i, f"Service=FMIP IP=10.7.{i}.1")
    webapi.requests_log.clear()

    out = tool.triage_devices_networks(SOURCE_ID)

    paths = [entry["path"] for entry in webapi.requests_log]
    searches = paths.count("/search")
    docs = sum("/docs/" in p for p in paths)
    assert searches == 4
    assert out["counters"]["http_calls"] == searches + docs == 4 + 3
    assert out["truncated"]["http_budget_exceeded"] is False


# --- (h) registro MCP -----------------------------------------------------------------------


def test_tool_registrada_via_mcp(fake_router: Any, webapi: Any) -> None:
    """triage_devices_networks é descoberta e chamada pelo MCP."""
    from fastmcp import Client, FastMCP

    from iped_mcp.triage import register as register_triage

    server = FastMCP("iped_mcp_test")
    register_triage(server)

    async def _run() -> dict[str, Any]:
        async with Client(server) as client:
            names = [t.name for t in await client.list_tools()]
            assert "triage_devices_networks" in names
            result = await client.call_tool(
                "triage_devices_networks", {"source_id": SOURCE_ID}
            )
            return (
                result.data
                if result.data is not None
                else json.loads(result.content[0].text)
            )

    payload = asyncio.run(_run())
    assert payload["source_id"] == SOURCE_ID
    assert payload["message"] == knowledge.DN_EMPTY_MESSAGE
