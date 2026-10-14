"""T-3 (DESIGN 09-B §3.10): triage_identity — identidade + reconhecidos.

Casos pinados: device_info Android com aliases (a), reconhecido por
contentType sem categoria que NÃO entra na identidade (b), teto 50 com
contador pré-teto honesto (c), os 5 filtros do Regex:PHONE + INT_MAX (d),
ALTA/MÉDIA/BAIXA com score 90/70/50 e relevância >=85/>=60 exatos (e),
contratos M-2 (source 404, query 500, eixo vazio) (f), truncamento da
amostra de contatos (g), pré-sonda de campos de nome com userName e
aviso de cobertura zero (h) e orçamento R-TRIAGE-1 com http_calls exato
(i/j). Backend: FakeWebAPI da suíte; nenhum HTTP real.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from iped_mcp import progress
from iped_mcp.triage import identity, knowledge, tool
from tests.conftest import SOURCE_ID, UNKNOWN_SOURCE, make_doc

CONTACTS_QUERY = 'category:"contacts"'
RECOGNIZED_QUERY = 'category:"recognized devices"'
ENVELOPE_KEYS = {
    "source_id",
    "recognized_devices",
    "recognized_devices_count",
    "query_hits",
    "counters",
    "truncated",
    "warnings",
}


def _add_doc(
    api: Any, doc_id: int, name: str, props: dict[str, Any],
    category: str | None = None,
) -> None:
    """Registra doc no fake (props no formato do jar: valores em lista).

    Args:
        api: estado observável do FakeWebAPI.
        doc_id: id do documento.
        name: nome do arquivo.
        props: propriedades (valores em lista).
        category: categoria canônica para o matcher category_hits (opcional;
            quando None o doc só é alcançado por outra query já registrada).
    """
    full = dict(props)
    if category is not None:
        full["category"] = [category]
        api.category_hits.setdefault(category, []).append(doc_id)
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, name, full)


def _add_recognized(api: Any, doc_id: int, model: str) -> None:
    """Reconhecido essencial (model) na categoria de reconhecidos."""
    _add_doc(
        api, doc_id, f"rec-{doc_id}.ufdr",
        {"ufed:DeviceModel": [model]}, category="recognized devices",
    )


def _http_paths(api: Any) -> list[str]:
    """Paths (sem query) de todos os pedidos recebidos pelo fake."""
    return [entry["path"] for entry in api.requests_log]


# --- (a) device_info Android: EntryName/EntryValue + aliases + "N/A" ---------


def test_device_info_android_padronizado_16_chaves(
    webapi: Any, fake_router: Any
) -> None:
    """EntryName/EntryValue da fonte nativa preenchem o schema de 16 chaves;
    valor "N/A" é ignorado (identity_strategy.py:42-53)."""
    _add_doc(
        webapi, 401, "native-dev.json",
        {
            "ufed:Source": ["Native"],
            "ufed:EntryName": [
                "Device name", "IMEI", "ICCID", "MSISDN", "OS Version", "Serial",
            ],
            "ufed:EntryValue": [
                "Moto G", "000000000000001", "8901260000000000000",
                "5511999990000", "Android 12", "N/A",
            ],
        },
        category="account",
    )

    out = tool.triage_identity(SOURCE_ID)

    device = out["identity"]["device_info"]
    assert set(device) == set(knowledge.DEVICE_INFO_SCHEMA)
    assert device["device_name"] == "Moto G"
    assert device["imei"] == "000000000000001"
    assert device["iccid"] == "8901260000000000000"
    assert device["msisdn"] == "5511999990000"
    assert device["os_version"] == "Android 12"
    assert device["serial"] == knowledge.DEVICE_INFO_UNKNOWN
    assert out["identity"]["evidence_source_type"] == knowledge.EVIDENCE_DEVICE


# --- (b) reconhecido por contentType: entidade própria -----------------------


def test_reconhecido_por_contenttype_nao_alimenta_identidade(
    webapi: Any, fake_router: Any
) -> None:
    """contentType recognizeddevice sem categoria: vira reconhecido e a
    identidade principal NÃO nasce dele (:166-170, :211-229)."""
    _add_doc(
        webapi, 402, "rec-item",
        {
            "contentType": ["application/x-recognizeddevice"],
            "ufed:DeviceModel": ["iPhone 12"],
            "ufed:SerialNumber": ["XYZ789"],
            "path": ["Nuvem GOOGLE pessoa_teste_13@example.invalid.ufdr/_DecodedData/x"],
        },
        category="recognized devices",
    )

    out = tool.triage_identity(SOURCE_ID)

    assert "identity" not in out and "message" not in out
    assert out["recognized_devices_count"] == 1
    rec = out["recognized_devices"][0]
    assert rec["modelo"] == "iPhone 12" and rec["serial"] == "XYZ789"
    assert rec["conta_nuvem"] == "pessoa_teste_13@example.invalid"
    assert rec["doc_id"] == 402
    assert out["counters"]["processed"] == 1


# --- (c) teto 50 com contador pré-teto honesto -------------------------------


def test_teto_de_reconhecidos_com_contador_honesto(
    webapi: Any, fake_router: Any
) -> None:
    """55 reconhecidos → entrega 50, count 55, truncamento marcado (T1-4)."""
    for i in range(55):
        _add_recognized(webapi, 500 + i, f"Modelo {i}")

    out = tool.triage_identity(SOURCE_ID)

    assert out["recognized_devices_count"] == 55
    assert len(out["recognized_devices"]) == knowledge.RECOGNIZED_DEVICES_MAX
    assert out["truncated"]["recognized_truncated"] is True
    assert out["truncated"]["recog_omitted"] == 5


# --- (d) Regex:PHONE: os 5 filtros + INT_MAX ----------------------------------


def test_regex_phone_filtros_e_int_max(webapi: Any, fake_router: Any) -> None:
    """Cada rejeição literal do ICARO (:266-278) e a janela aceita 10..13."""
    _add_doc(
        webapi, 601, "msg.db",
        {"Regex:PHONE": [
            "123.456",              # decimal
            "8987654321012",        # ICCID (89 + >=10)
            "12345",                # curto sem indicador
            "12 3456",              # espaço sem padrão de telefone
            "+21474836470",         # INT_MAX embutido (11 díg. c/ indicador)
            "551199999011",         # aceita (12 dígitos >= 11)
            "+55 11 99999-0021",    # aceita (espadado no padrão)
        ]},
        category="messages",
    )

    out = tool.triage_identity(SOURCE_ID)

    assert out["identity"]["mentioned_phone_numbers"] == [
        "551199999011", "+55 11 99999-0021",
    ]


# --- (e) scoring pinado 90/70/50 e relevância >=85/>=60 -----------------------


def test_confianca_alta_score_90_e_justificacao_literal(
    webapi: Any, fake_router: Any
) -> None:
    """ALTA só com fonte google/... + ufed:Name (:328-332); score/relevância
    e justificação exatos do handler (draft:845-862)."""
    _add_doc(
        webapi, 701, "account.json",
        {
            "ufed:Source": ["google"],
            "ufed:Name": ["Fulano da Silva (titular)"],
            "ufed:EmailAddress": ["pessoa_teste_9@gmail.example.invalid"],
        },
        category="user accounts",
    )

    out = tool.triage_identity(SOURCE_ID)

    ident = out["identity"]
    assert ident["name"] == "Fulano da Silva"  # "(...)" limpo (:455)
    assert (ident["confidence"], ident["score"], ident["relevance"]) == (
        "ALTA", 90, "ALTA",
    )
    assert ident["justification"] == (
        "Entidade de identidade (confiança: ALTA) | E-mails: pessoa_teste_9@gmail.example.invalid"
    )
    assert ident["evidence_source_type"] == knowledge.EVIDENCE_GOOGLE
    assert ident["device_info"]["google_account"] == "pessoa_teste_9@gmail.example.invalid"


def test_fonte_nao_alta_confianca_media_score_70(
    webapi: Any, fake_router: Any
) -> None:
    """Fonte fora da lista ALTA: nome via fallback, MÉDIA→70→MÉDIA (:847/:856)."""
    _add_doc(
        webapi, 702, "account2.json",
        {"ufed:Source": ["skype"], "ufed:Name": ["Ciclano"]},
        category="account",
    )

    ident = tool.triage_identity(SOURCE_ID)["identity"]

    assert (ident["confidence"], ident["score"], ident["relevance"]) == (
        "MÉDIA", 70, "MÉDIA",
    )
    assert ident["justification"] == "Entidade de identidade (confiança: MÉDIA)"
    assert ident["evidence_source_type"] == knowledge.EVIDENCE_UNKNOWN


def test_score_50_for_confianca_baixa_unidade() -> None:
    """Pin do handler: confiança fora do mapa (BAIXA/ausente) → 50/BAIXA."""
    payload = identity._identity_payload(
        identity._IdentityAgg(confidence=knowledge.RELEVANCE_BAIXA)
    )
    assert (payload["score"], payload["relevance"]) == (50, "BAIXA")
    assert payload["name"] == knowledge.NOT_IDENTIFIED_NAME


# --- (f) contratos M-2 --------------------------------------------------------


def test_source_inexistente_vira_value_error_estavel(fake_router: Any) -> None:
    """404 de source: ValueError estável ANTES de qualquer HTTP (M-2)."""
    with pytest.raises(ValueError, match="não encontrado em nenhum endpoint"):
        tool.triage_identity(UNKNOWN_SOURCE)


def test_eixo_vazio_e_mensagem_explicita_sem_excecao(
    webapi: Any, fake_router: Any
) -> None:
    """Nada no eixo: message explícita, sem identity, sem exceção (§0.1)."""
    out = tool.triage_identity(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS | {"message"}
    assert out["message"] == knowledge.IDENTITY_EMPTY_MESSAGE
    assert "identity" not in out
    assert out["counters"]["http_calls"] == len(knowledge.IDENTITY_CATEGORIES)
    assert out["warnings"] == []


def test_query_500_vira_hits_menos_um_e_eixo_segue(
    webapi: Any, fake_router: Any
) -> None:
    """Falha de UMA query: -1 + warning; as demais seguem normal (M-3).

    A query que falha (``contacts``) é justamente a única que traria o doc
    de contato: ela é re-tentada pela variante irmã ``contatos``, que o
    fake devolve a partir de category_hits próprio (o eixo continua)."""
    webapi.fail_search_queries = {CONTACTS_QUERY}
    _add_doc(
        webapi, 703, "c.vcf",
        {"ufed:Name": ["Ana"], "ufed:PhoneNumber": ["5511999990005"]},
        category="contatos",
    )
    webapi.category_hits[CONTACTS_QUERY.split('"')[1]] = [703]

    out = tool.triage_identity(SOURCE_ID)

    assert out["query_hits"][CONTACTS_QUERY] == -1
    assert any(CONTACTS_QUERY in w for w in out["warnings"])
    assert out["identity"]["known_contacts_count"] == 1


# --- (g) truncamento da amostra de contatos -----------------------------------


def test_amostra_da_agenda_limitada_e_marcada(webapi: Any, fake_router: Any) -> None:
    """25 contatos: amostra 20, count 25, truncated marcado (§3.7)."""
    for i in range(25):
        _add_doc(
            webapi, 750 + i, f"c{i}.vcf",
            {"ufed:Name": [f"Contato {i}"], "ufed:PhoneNumber": [f"5511900{i:05d}"]},
            category="contatos",
        )

    out = tool.triage_identity(SOURCE_ID)

    assert out["identity"]["known_contacts_count"] == 25
    assert len(out["identity"]["known_contacts_sample"]) == knowledge.KNOWN_CONTACTS_SAMPLE
    assert out["truncated"]["known_contacts_truncated"] is True
    assert out["truncated"]["known_contacts_omitted"] == 5


# --- (h) pré-sonda de campos de nome ------------------------------------------


def test_pre_sonda_histograma_com_userName(webapi: Any, fake_router: Any) -> None:
    """userName coberto no build: histograma no envelope + nome via fallback."""
    _add_doc(webapi, 780, "wa.json", {"userName": ["Nick do Zap"]}, category="user accounts")

    out = tool.triage_identity(SOURCE_ID)

    assert out["counters"]["name_field_hits"]["userName"] == 1
    assert out["identity"]["name"] == "Nick do Zap"


def test_pre_sonda_cobertura_zero_avisa_com_literais(
    webapi: Any, fake_router: Any
) -> None:
    """Docs varridos sem NENHUMA variante: aviso nomeia os testados (§3.2)."""
    _add_doc(webapi, 781, "dev.json", {"ufed:Source": ["native"]}, category="device info")

    out = tool.triage_identity(SOURCE_ID)

    assert sum(out["counters"]["name_field_hits"].values()) == 0
    coverage = [w for w in out["warnings"] if "sem cobertura" in w]
    assert len(coverage) == 1
    assert "ufed:userName" in coverage[0]  # lista os literais testados


# --- (i/j) orçamento R-TRIAGE-1 ------------------------------------------------


def test_orcamento_estourado_vira_parcial_marcado(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap atingido: NUNCA exceção — phase partial, truncated, warning com
    cap e o que ficou de fora; resultado parcial NÃO vazio (§2.3/§2.5a)."""
    monkeypatch.setitem(knowledge.TOOL_BUDGETS, "triage_identity", 21)
    for i in range(5):
        _add_doc(
            webapi, 801 + i, f"c{i}.vcf",
            {"ufed:Name": [f"Contato {i}"], "ufed:PhoneNumber": [f"5511900{i:05d}"]},
            category="contacts",
        )

    out = tool.triage_identity(SOURCE_ID)

    counters, truncated = out["counters"], out["truncated"]
    assert (counters["phase"], counters["http_calls"]) == (
        progress.PHASE_PARTIAL, 21,
    )
    assert (
        truncated["http_budget_exceeded"],
        truncated["http_budget_calls_at_stop"],
    ) == (True, 21)
    assert any("http_budget_exceeded" in w and "cap 21" in w for w in out["warnings"])
    # 5 candidatos, RESTANTE=2: parcial não vazio
    assert (counters["docs_fetched"], out["identity"]["known_contacts_count"]) == (2, 2)


def test_http_calls_exato_e_igual_ao_trafego_do_fake(
    webapi: Any, fake_router: Any
) -> None:
    """Pino de exatidão: http_calls == /search + /docs efetivamente pagos."""
    _add_doc(webapi, 820, "c.vcf", {"ufed:Name": ["Ana"], "ufed:PhoneNumber": ["5511999990005"]}, category="contatos")
    _add_recognized(webapi, 821, "Pixel 7")
    webapi.requests_log.clear()

    out = tool.triage_identity(SOURCE_ID)

    paths = _http_paths(webapi)
    searches = paths.count("/search")
    docs = sum("/docs/" in p for p in paths)
    assert searches == len(knowledge.IDENTITY_CATEGORIES)
    assert out["counters"]["http_calls"] == searches + docs == 19 + 2
    assert out["truncated"]["http_budget_exceeded"] is False


# --- registro MCP --------------------------------------------------------------


def test_tool_registrada_via_mcp(fake_router: Any, webapi: Any) -> None:
    """triage_identity é descoberta e chamada pelo MCP (mesmo mecanismo)."""
    from fastmcp import Client, FastMCP

    from iped_mcp.triage import register as register_triage

    server = FastMCP("iped_mcp_test")
    register_triage(server)

    async def _run() -> dict[str, Any]:
        async with Client(server) as client:
            names = [t.name for t in await client.list_tools()]
            assert "triage_identity" in names
            result = await client.call_tool("triage_identity", {"source_id": SOURCE_ID})
            return result.data if result.data is not None else json.loads(result.content[0].text)

    payload = asyncio.run(_run())
    assert payload["source_id"] == SOURCE_ID
