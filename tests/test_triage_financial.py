"""T-4 (DESIGN 09-B §4.8): triage_financial — evidências, cartões, contas.

Casos pinados: parser BR com sentinela (a), escada method/amount + dedup
por assinatura + "transferencia" sem acento + mídia sem termo descartada
MESMO com regex (b), sondas de detectores por caso com warning nominal e
500 sem exceção (c), score de cartão min(60+2tx,95) com ALTA>=85 e amostra
de transações (d), contas/ativos com agregados (e), handler PIX→ALTA (f),
contratos M-2 (source 404, query 500, eixo vazio) (g), tetos com omissão
contada (h) e orçamento R-TRIAGE-1 com http_calls exato (i). Backend:
FakeWebAPI da suíte; nenhum HTTP real.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from iped_mcp import progress
from iped_mcp.triage import financial, knowledge, tool
from tests.conftest import SOURCE_ID, UNKNOWN_SOURCE, make_doc

UUID_A = "12345678-1234-1234-1234-123456789abc"
PROBE_FAIL_MONEY = "Regex\\:MONEY:*"  # forma crua que o fake compara (500)
ENVELOPE_KEYS = {
    "source_id",
    "evidences",
    "evidence_count",
    "cards",
    "accounts",
    "account_count",
    "regex_availability",
    "query_hits",
    "counters",
    "truncated",
    "warnings",
}


def _add_doc(
    api: Any, doc_id: int, name: str, props: dict[str, Any],
    category: str | None = None, path: str | None = None,
    probe_category: str | None = None,
) -> None:
    """Registra doc no fake (props no formato do jar: valores em lista).

    Args:
        api: estado observável do FakeWebAPI.
        doc_id: id do documento.
        name: nome do arquivo.
        props: propriedades (valores em lista).
        category: categoria REAL do doc (propriedade ``category``).
        path: caminho completo (regex de UUID/e-mail são sensíveis a
            maiúsculas — grava-se o path cru).
        probe_category: categoria sob a qual a query ``category:"X"`` do
            fake devolve o doc (quando difere da real — permite modelar o
            gate por substring sem registrar 18 categorias no fake).
    """
    full = dict(props)
    if category is not None:
        full["category"] = [category]
    if path is not None:
        full["knowledge_path"] = [path]
        full["path"] = [path]
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, name, full)
    if category is not None or probe_category is not None:
        api.category_hits.setdefault(
            probe_category or category or "", []
        ).append(doc_id)


def _money(api: Any, doc_id: int, name: str, values: list[str],
           category: str = "documentos") -> None:
    """Doc de evidência Regex:MONEY na categoria financeira padrão."""
    _add_doc(api, doc_id, name, {"Regex:MONEY": values}, category=category)


def _http_paths(api: Any) -> list[str]:
    """Paths (sem query) de todos os pedidos recebidos pelo fake."""
    return [entry["path"] for entry in api.requests_log]


# --- (a) parser BR de valores (:206-241) --------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.234", 1234.0),          # ponto de milhar (último bloco de 3)
        ("1.23", 1.23),             # sem vírgula, último bloco != 3 → decimal
        ("R$ 1.234,56", 1234.56),   # BR completo
        ("1.234.567", 1234567.0),   # dois pontos de milhar
        ("50", 50.0),
        ("Boleto Bancário", -1.0),  # marcador → sentinela
        ("Valor não identificado", -1.0),
        ("", -1.0),                 # vazio → sentinela (fiel a :215)
        (None, -1.0),               # não-str → sentinela
    ],
)
def test_parser_valor_br_pinado(raw: Any, expected: float) -> None:
    """O parser porta a regra literal do ICARO, sentinela -1.0 inclusa."""
    assert financial._parse_amount(raw) == expected


# --- (b) escada, assinatura, quirks de gate -----------------------------------


def test_escada_method_e_amount_do_ladder(webapi: Any, fake_router: Any) -> None:
    """Escada fiel MONEY > Boleto > Conta (:183-191) com scores do handler."""
    _money(webapi, 10, "comprovante.pdf", ["R$ 1.234,56"])
    _add_doc(webapi, 12, "boleto.pdf", {"Regex:BR_BOLETO": ["linha"]},
             category="documentos")
    _add_doc(webapi, 13, "extrato.txt",
             {"Regex:BR_BANK_ACCOUNT": ["Ag 1234\nConta 5"]},
             category="documentos")

    out = tool.triage_financial(SOURCE_ID)

    by_doc = {e["doc_id"]: e for e in out["evidences"]}
    assert (by_doc[10]["method"], by_doc[10]["amount"]) == (
        "Dinheiro/Valor Encontrado", "R$ 1.234,56",
    )
    assert (by_doc[12]["method"], by_doc[12]["amount"]) == (
        "Boleto", "Boleto Bancário",
    )
    assert (by_doc[13]["method"], by_doc[13]["amount"]) == (
        "Comprovante/Conta", "Valor não identificado",
    )


def test_scores_do_handler_de_evidencia(webapi: Any, fake_router: Any) -> None:
    """Handler :886 pontua pelo AMOUNT: 75 com valor (inclusive o literal
    "Boleto Bancário", que NÃO é o sentinel), 55 só para o sentinel."""
    _money(webapi, 10, "comprovante.pdf", ["R$ 1.234,56"])
    _add_doc(webapi, 12, "boleto.pdf", {"Regex:BR_BOLETO": ["linha"]},
             category="documentos")
    _add_doc(webapi, 13, "extrato.txt", {"Regex:BR_BANK_ACCOUNT": ["Ag 1"]},
             category="documentos")

    out = tool.triage_financial(SOURCE_ID)

    scores = {e["doc_id"]: e["score"] for e in out["evidences"]}
    assert scores == {10: 75, 12: 75, 13: 55}


def test_dedup_por_assinatura_e_beneficiary_colapsado(
    webapi: Any, fake_router: Any
) -> None:
    """Dedup por (money[0], conta[0]) (:172) e beneficiary com quebras
    colapsadas (:194-196)."""
    _money(webapi, 10, "comprovante_1.pdf", ["R$ 1.234,56"])
    _money(webapi, 11, "comprovante_2.pdf", ["R$ 1.234,56"])  # MESMA assinatura
    _add_doc(webapi, 13, "extrato.txt",
             {"Regex:BR_BANK_ACCOUNT": ["Ag 1234\nConta 5"]},
             category="documentos")

    out = tool.triage_financial(SOURCE_ID)

    assert len(out["evidences"]) == 2  # doc 11 engolido pela assinatura
    assert [e["doc_id"] for e in out["evidences"]] == [10, 13]
    assert out["evidences"][1]["beneficiary"] == "Ag 1234 Conta 5"


def test_evidencias_ordenadas_por_valor_desc(webapi: Any, fake_router: Any) -> None:
    """Ordenação por valor parseado desc (ICARO :201); não-numérico vai ao fim."""
    _money(webapi, 20, "a.pdf", ["R$ 50,00"])
    _money(webapi, 21, "b.pdf", ["R$ 1.234,56"])
    _add_doc(webapi, 22, "c.pdf", {"Regex:BR_BOLETO": ["x"]}, category="documentos")

    out = tool.triage_financial(SOURCE_ID)

    assert [e["doc_id"] for e in out["evidences"]] == [21, 20, 22]


def test_transferencia_acentuada_precisa_de_regex_e_sem_acento_nao(webapi: Any, fake_router: Any) -> None:
    """QUIRK fiel (:56): o termo é "transferencia" sem acento — o nome
    acentuado só passa pelo regex; o sem acento passa pelo termo sozinho."""
    _add_doc(webapi, 30, "transferência.pdf", {}, category="pdf")
    _money(webapi, 31, "transferência_comprovada.pdf", ["R$ 9,00"], category="pdf")
    _add_doc(webapi, 32, "transferencia.pdf", {}, category="pdf")

    out = tool.triage_financial(SOURCE_ID)

    assert {e["doc_id"] for e in out["evidences"]} == {31}
    # doc 32 passou no gate (termo) mas não tem regex: entra como documento
    # classificado sem evidência (counters), doc 30 nem entrou no eixo.
    assert out["counters"]["docs_classified"] == 2


def test_midia_sem_termo_descartada_mesmo_com_regex(
    webapi: Any, fake_router: Any
) -> None:
    """Anti-ruído fiel (:61-63): mídia sem termo no nome é descartada MESMO
    com Regex:MONEY; com termo no nome, passa."""
    _money(webapi, 40, "IMG_0042.jpg", ["R$ 100,00"], category="images")
    _money(webapi, 41, "comprovante_0042.jpg", ["R$ 100,00"], category="images")

    out = tool.triage_financial(SOURCE_ID)

    assert [e["doc_id"] for e in out["evidences"]] == [41]


# --- (c) sondas de detectores por caso (§4.3, M-7) ----------------------------


def test_sonda_sem_detectores_vira_warning_nominal(
    webapi: Any, fake_router: Any
) -> None:
    """Detector com 0 hits no caso: painel 0 + warning nomeando o detector;
    eixo segue sem exceção (a lista é o consumo do ICARO, não o catálogo)."""
    out = tool.triage_financial(SOURCE_ID)

    assert out["regex_availability"] == {
        "MONEY": 0, "BR_BOLETO": 0, "BR_BANK_ACCOUNT": 0, "CREDIT_CARD": 0,
    }
    for name in knowledge.FINANCIAL_REGEX_PROBES:
        assert any(
            f"detectorRegex:{name} indisponível neste caso (0 hits)" in w
            for w in out["warnings"]
        )


def test_sonda_500_nao_derruba_o_eixo(webapi: Any, fake_router: Any) -> None:
    """Probe 500: painel registra -1, warning nominal, eixo continua (§4.3/M-3)."""
    webapi.fail_search_queries.add(PROBE_FAIL_MONEY)
    _money(webapi, 42, "comprovante.pdf", ["R$ 10,00"])

    out = tool.triage_financial(SOURCE_ID)

    assert out["regex_availability"]["MONEY"] == -1
    assert any("sonda Regex:MONEY falhou" in w for w in out["warnings"])
    assert [e["doc_id"] for e in out["evidences"]] == [42]
    assert out["regex_availability"]["BR_BOLETO"] == 0


# --- (d) cartões: score do handler + jumps + amostra --------------------------


def _add_card_flow(api: Any, tx_count: int) -> None:
    """Cartão de crédito com N transações via jumpTargets (fluxo :243-334)."""
    jumps = [str(100 + i) for i in range(tx_count)]
    _add_doc(
        api, 90, "CreditCard_Nubank.ufdr",
        {
            "ufed:CreditCardNumber": ["4000-00**-****-1234"],
            "ufed:Company": ["Nubank"],
            "ufed:ExpirationDate": ["12/28"],
            "ufed:Source": ["Google Pay"],
            "ufed:jumpTargets": jumps,
        },
        category="credit cards",
    )
    for i in range(tx_count):
        _add_doc(
            api, 100 + i, f"tx_{i}.ufdr",
            {
                "ufed:id": [str(100 + i)],
                "ufed:Amount": ["10,50"],
                "ufed:Currency": ["BRL"],
                "ufed:DateProcessed": [f"2024-05-{i:02d}"],
                "ufed:Status": ["APPROVED"],
                "ufed:TransferType": ["PURCHASE"],
                "Communication:To": ["Loja X"],
                "Communication:From": ["titular"],
            },
            category="transfers of funds",
        )


def test_cartao_score_86_e_fluxo_de_jumps(webapi: Any, fake_router: Any) -> None:
    """13 transações: score min(60+26,95)=86 → ALTA (>=85); total somado."""
    _add_card_flow(webapi, 13)

    out = tool.triage_financial(SOURCE_ID)

    assert len(out["cards"]) == 1
    card = out["cards"][0]
    assert (card["score"], card["relevance"]) == (86, knowledge.RELEVANCE_ALTA)
    assert card["transactions_count"] == 13
    assert card["total_spent"] == pytest.approx(136.5)
    assert card["entity_name"] == "Credit Card: Nubank"
    assert card["justification"] == "13 transações | Total: R$ 136.50"
    assert card["source_wallet"] == "Google Pay"


def test_cartao_amostra_de_transacoes_marcada(webapi: Any, fake_router: Any) -> None:
    """13 transações: amostra das 5 últimas no envelope, omissão marcada (§4.6)."""
    _add_card_flow(webapi, 13)

    card = tool.triage_financial(SOURCE_ID)["cards"][0]

    assert len(card["transactions"]) == knowledge.CARD_TRANSACTIONS_SAMPLE
    assert card["transactions"][-1]["date"] == "2024-05-12"  # última descoberta
    assert card["transactions_truncated"] is True
    assert card["transactions_omitted"] == 8


@pytest.mark.parametrize(
    ("tx_count", "score", "relevance"),
    [(0, 60, knowledge.RELEVANCE_MEDIA), (12, 84, knowledge.RELEVANCE_MEDIA),
     (13, 86, knowledge.RELEVANCE_ALTA), (20, 95, knowledge.RELEVANCE_ALTA)],
)
def test_cartao_score_pinado_do_handler(
    tx_count: int, score: int, relevance: str
) -> None:
    """Pino direto do handler :901-905: base 60, +2/tx, cap 95, ALTA >= 85."""
    card = {
        "card_type": "Credit Card", "description": "", "masked_number": "4111",
        "expiration": "", "added_date": "", "source_wallet": "",
        "doc_id": 1, "path": "", "locations": [], "total_spent": 0.0,
        "transactions": [{"amount": 1.0}] * tx_count,
    }

    out = financial._finding_from_card(card)

    assert (out["score"], out["relevance"]) == (score, relevance)


def test_cartao_wallet_fallback_literal(webapi: Any, fake_router: Any) -> None:
    """Cartão sem ufed:Source: carteira cai no literal 'Carteira digital' (:908)."""
    _add_doc(webapi, 95, "Mobile Card Wallet Card - Google.csv",
             {"ufed:Description": ["Google Wallet"]}, category="mobile cards")

    out = tool.triage_financial(SOURCE_ID)

    assert out["cards"][0]["source_wallet"] == "Carteira digital"
    assert out["cards"][0]["card_type"] == "Mobile Card"


# --- (e) contas/ativos (:106-159) ---------------------------------------------


def test_conta_com_ativos_e_conta_de_nuvem(webapi: Any, fake_router: Any) -> None:
    """Conta estruturada com moedas/contagem agregadas dos ativos e a conta
    de nuvem resolvida pelo e-mail .ufdr do path (:21-22/:139-157)."""
    _add_doc(
        webapi, 200, "FinancialAccount_A.ufdr",
        {
            "ufed:id": [UUID_A],
            "ufed:AccountID": ["ag-1"],
            "ufed:FinancialAccountType": ["BANCO"],
            "ufed:Source": ["Nubank"],
            "ufed:decoding_confidence": ["alta"],
            "contentType": ["application/x-ufed-financialaccount"],
        },
        category="financial accounts", path=f"cont/{UUID_A}.ufdr",
        probe_category="financial account",
    )
    for i, coin in enumerate(["BRL", "BRL", "USD"]):
        _add_doc(
            webapi, 210 + i, f"asset_{i}.json",
            {
                "ufed:Currency": [coin],
                "contentType": ["application/x-ufed-financialasset"],
            },
            category="financial account",
            path=f"cont/FinancialAccount_{UUID_A}/asset_{i}",
        )
    _add_doc(webapi, 220, "contas_contato.ufdr",
             {"ufed:id": ["conta-nuvem-1"]}, category="financial accounts",
             path="e-mails/pessoa_teste_9@gmail.example.invalid.ufdr",
             probe_category="financial account")

    out = tool.triage_financial(SOURCE_ID)

    by_id = {a["ufed_id"]: a for a in out["accounts"]}
    conta = by_id[UUID_A]
    assert conta["account_type"] == "BANCO"
    assert conta["currencies"] == ["BRL", "USD"]
    assert conta["asset_count"] == 3
    assert (conta["score"], conta["relevance"]) == (90, knowledge.RELEVANCE_ALTA)
    assert by_id["conta-nuvem-1"]["source_account"] == "pessoa_teste_9@gmail.example.invalid"


# --- (f) handler da evidência: PIX→ALTA (§4.5) ---------------------------------


def test_pix_no_method_vira_alta_senao_media() -> None:
    """Pino do handler :886-887 (a escada do strategy nunca produz "PIX" no
    method — a regra é do handler genérico e vale para qualquer method)."""
    base = {
        "amount": "R$ 10,00", "beneficiary": "N/I", "doc_id": 1,
        "path": "", "source_name": "x.pdf",
    }

    pix = financial._finding_from_evidence({**base, "method": "PIX"})
    comum = financial._finding_from_evidence({**base, "method": "Boleto"})

    assert (pix["relevance"], pix["score"]) == (knowledge.RELEVANCE_ALTA, 75)
    assert comum["relevance"] == knowledge.RELEVANCE_MEDIA
    assert (comum["entity_name"], comum["justification"]) == (
        "Boleto — N/I", "Valor: R$ 10,00 | Método: Boleto",
    )


# --- (g) contratos M-2 ---------------------------------------------------------


def test_source_inexistente_vira_value_error_estavel(fake_router: Any) -> None:
    """404 de source: ValueError estável ANTES de qualquer HTTP (M-2)."""
    with pytest.raises(ValueError, match="não encontrado em nenhum endpoint"):
        tool.triage_financial(UNKNOWN_SOURCE)


def test_query_de_categoria_500_vira_hit_menos_um_e_eixo_segue(
    webapi: Any, fake_router: Any
) -> None:
    """500 em query de categoria: query_hits -1 + warning; nada levanta (M-3)."""
    webapi.fail_search_queries.add('category:"documentos"')
    _money(webapi, 50, "comp.pdf", ["R$ 1,00"])

    out = tool.triage_financial(SOURCE_ID)

    assert out["query_hits"]['category:"documentos"'] == -1
    assert any('category:"documentos"' in w for w in out["warnings"])


def test_eixo_vazio_e_mensagem_explicita_sem_excecao(
    webapi: Any, fake_router: Any
) -> None:
    """Nada no eixo: message explícita, sem exceção (§0.1)."""
    out = tool.triage_financial(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS | {"message"}
    assert out["message"] == knowledge.FINANCIAL_EMPTY_MESSAGE
    assert out["evidences"] == [] and out["cards"] == [] and out["accounts"] == []
    # 18 queries de categoria + 4 sondas de detector
    assert out["counters"]["http_calls"] == 22


# --- (h) tetos com omissão contada (§4.6) --------------------------------------


def test_teto_de_evidencias_marcado_com_omissao(
    webapi: Any, fake_router: Any
) -> None:
    """35 evidências: entrega 30, evidence_count 35, truncated com 5 omitidas."""
    for i in range(knowledge.FINANCIAL_LIST_TOP + 5):
        _money(webapi, 300 + i, f"comp_{i}.pdf", [f"R$ {i + 1},00"])

    out = tool.triage_financial(SOURCE_ID)

    assert len(out["evidences"]) == knowledge.FINANCIAL_LIST_TOP
    assert out["evidence_count"] == 35
    assert out["truncated"]["evidences_truncated"] is True
    assert out["truncated"]["evidences_omitted"] == 5


# --- (i) orçamento R-TRIAGE-1 ---------------------------------------------------


def test_orcamento_estourado_vira_parcial_marcado(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap atingido: nunca exceção — phase partial + warning com cap (§2.3).
    Cap 23 = 18 categorias + 4 sondas + 1 doc: resultado parcial não vazio."""
    monkeypatch.setitem(knowledge.TOOL_BUDGETS, "triage_financial", 23)
    _money(webapi, 400, "a.pdf", ["R$ 5,00"])
    _money(webapi, 401, "b.pdf", ["R$ 6,00"])

    out = tool.triage_financial(SOURCE_ID)

    counters, truncated = out["counters"], out["truncated"]
    assert (counters["phase"], counters["http_calls"]) == (
        progress.PHASE_PARTIAL, 23,
    )
    assert truncated["http_budget_exceeded"] is True
    assert any("http_budget_exceeded" in w and "cap 23" in w for w in out["warnings"])
    assert counters["docs_fetched"] == 1
    assert out["evidence_count"] >= 1


def test_http_calls_exato_e_igual_ao_trafego_do_fake(
    webapi: Any, fake_router: Any
) -> None:
    """Pino de exatidão: 22 buscas (18+4) + N docs == http_calls == fake."""
    _money(webapi, 410, "a.pdf", ["R$ 5,00"])
    _add_card_flow(webapi, 1)
    webapi.requests_log.clear()

    out = tool.triage_financial(SOURCE_ID)

    paths = _http_paths(webapi)
    searches = paths.count("/search")
    docs = sum("/docs/" in p for p in paths)
    assert searches == 22
    assert out["counters"]["http_calls"] == searches + docs == 22 + 3
    assert out["truncated"]["http_budget_exceeded"] is False


# --- registro MCP --------------------------------------------------------------


def test_tool_registrada_via_mcp(fake_router: Any, webapi: Any) -> None:
    """triage_financial é descoberta e chamada pelo MCP (mesmo mecanismo)."""
    from fastmcp import Client, FastMCP

    from iped_mcp.triage import register as register_triage

    server = FastMCP("iped_mcp_test")
    register_triage(server)

    async def _run() -> dict[str, Any]:
        async with Client(server) as client:
            names = [t.name for t in await client.list_tools()]
            assert "triage_financial" in names
            result = await client.call_tool("triage_financial", {"source_id": SOURCE_ID})
            return result.data if result.data is not None else json.loads(result.content[0].text)

    payload = asyncio.run(_run())
    assert payload["source_id"] == SOURCE_ID
    assert payload["message"] == knowledge.FINANCIAL_EMPTY_MESSAGE
