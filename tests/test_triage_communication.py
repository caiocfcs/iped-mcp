"""Testes do eixo comunicação (T-8, DESIGN 09-B §8).

Cobrem: (a) happy path de chats/interação/busca; (b) pontuação com termos
pinada no handler do ICARO (min(50+total,95), draft_report:868) e (c) sem
termos (min(40+total//2,70), MÉDIA só com total > 50, :869); (d) dedup de
queries — o eixo IMPORTA o builder WhatsApp (prova: patch no builder muda a
query emitida); (e) tetos (indivíduos top-20, grupos sempre, visão [:30],
histórico [:50], CHAT_SCAN_CAP) todos marcados; (f) contratos M-2 (500 por
query, 404, vazio) e orçamento R-TRIAGE-1 com http_calls exato. Backend:
FakeWebAPI; nenhum HTTP real, nenhum LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from iped_mcp.triage import communication as comm
from iped_mcp.triage import knowledge
from iped_mcp.whatsapp import queries as wa_queries
from tests.conftest import SOURCE_ID, UNKNOWN_SOURCE, make_doc

ENVELOPE_KEYS = {
    "source_id",
    "chats",
    "searches",
    "chats_individuals_total",
    "chats_grupos_total",
    "query_hits",
    "counters",
    "truncated",
    "warnings",
}
WA_QUERY = "accountType:WhatsApp"  # valor atual do builder (pino indireto (d))


def _add_doc(
    api: Any, doc_id: int, props: dict[str, list[Any]], category: str, name: str = "d"
) -> None:
    """Registra doc no fake sob a categoria canônica ``category:"<cat>"``.

    Args:
        api: estado observável do FakeWebAPI.
        doc_id: id do documento.
        props: propriedades (valores em lista, formato do jar).
        category: categoria que alimenta o matcher ``category:"<cat>"``.
        name: nome do arquivo.

    Returns:
        Nada.
    """
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, name, {"category": [category], **props})
    api.category_hits.setdefault(category, []).append(doc_id)


def _chats_by_name(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Índice ``interlocutor -> registro`` da visão de chats."""
    return {chat["interlocutor"]: chat for chat in out["chats"]}


# --- (a) happy path ---------------------------------------------------------------


def test_mensagem_normal_vira_chat_normalizado(
    webapi: Any, fake_router: Any
) -> None:
    """Communication:To "Maria (+5511..." -> "MARIA (5511...)" (strategy:475-480)."""
    _add_doc(
        webapi,
        101,
        {"Communication:To": ["Maria (+5511999)"], "isGroupMessage": ["false"]},
        "whatsapp",
    )

    out = comm.build_triage_communication(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS
    maria = _chats_by_name(out)["MARIA"]  # normalização corta o telefone (:516)
    assert maria["is_group"] is False
    assert maria["message_count"] == 1
    assert maria["relevance"] == knowledge.RELEVANCE_BAIXA
    assert any("agregada-v1" in w for w in out["warnings"])


def test_grupo_mantem_nome_sem_id_interno(webapi: Any, fake_router: Any) -> None:
    """Grupo: nome até ``(id:`` em maiúsculas (:473); grupo não vazio conta."""
    _add_doc(
        webapi,
        102,
        {"Communication:To": ["Operações (id:123)@g.us"], "isGroupMessage": ["true"]},
        "chats",
    )

    out = comm.build_triage_communication(SOURCE_ID)

    assert _chats_by_name(out)["OPERAÇÕES"]["is_group"] is True
    assert out["chats_grupos_total"] == 1
    assert out["chats_individuals_total"] == 0


def test_dono_nunca_e_interlocutor(webapi: Any, fake_router: Any) -> None:
    """Nome == PhoneOwner:name -> DESCONHECIDO (:479-482/:519-520)."""
    _add_doc(
        webapi,
        103,
        {
            "Communication:To": ["Jose (+5521)"],
            "Conversation:PhoneOwner:name": ["Jose"],
            "Conversation:PhoneOwner:phoneNumber": ["+5521"],
        },
        "chats",
    )

    out = comm.build_triage_communication(SOURCE_ID)

    assert list(_chats_by_name(out)) == [knowledge.COMMUNICATION_UNKNOWN_LABEL]


def test_log_interacao_parser_literal(webapi: Any, fake_router: Any) -> None:
    """InteractionC: regex ``participent identifier:`` (sic, :408) vira chat;
    o nome do log NÃO passa pela normalização (:414 mantém o identifier)."""
    _add_doc(
        webapi,
        104,
        {
            "ufed:Source": ["InteractionC"],
            "ufed:Body": ["participent identifier: grupo_teste_1@g.us from Chefia"],
        },
        "logs",
    )
    _add_doc(
        webapi,
        105,
        {
            "ufed:Source": ["InteractionC"],
            "ufed:Body": ["participent identifier: conta_teste_1@s.whatsapp.net from Carlos"],
        },
        "logs",
    )

    out = comm.build_triage_communication(SOURCE_ID)

    chats = _chats_by_name(out)
    assert chats["CHEFIA"]["is_group"] is True
    assert chats["CARLOS (conta_teste_1@s.whatsapp.net)"]["is_group"] is False


def test_busca_do_navegador_ordenada_e_com_fallback(
    webapi: Any, fake_router: Any
) -> None:
    """Busca: ufed:Value + timestamp desc (:742) e "N/I" sem timestamp (:686)."""
    _add_doc(
        webapi,
        106,
        {
            "ufed:Source": ["chrome"],
            "ufed:Value": ["comprar pix"],
            "ufed:TimeStamp": ["2026-02-01"],
        },
        "web history",
    )
    _add_doc(
        webapi,
        107,
        {"ufed:Source": ["safari"], "ufed:Value": ["voos"]},
        "navegação",
    )
    long_value = "x" * 80
    _add_doc(
        webapi,
        108,
        {"ufed:Source": ["firefox"], "ufed:Value": [long_value],
         "ufed:TimeStamp": ["2026-04-01"]},
        "browser history",
    )

    out = comm.build_triage_communication(SOURCE_ID)

    # sort lexicográfico fiel ao ICARO (:742): "N/I" (maiúscula) vem antes
    # de "2026-..." — comportamento portado, não bug
    assert [s["query"] for s in out["searches"]] == [
        "voos",
        long_value[: knowledge.COMM_QUERY_TRUNC_CHARS],
        "comprar pix",
    ]
    assert [s["timestamp"] for s in out["searches"]] == [
        knowledge.COMMUNICATION_TIMESTAMP_FALLBACK,
        "2026-04-01",
        "2026-02-01",
    ]
    first = out["searches"][0]
    assert first["timestamp"] == knowledge.COMMUNICATION_TIMESTAMP_FALLBACK
    assert first["source_app"] == "safari"
    assert first["score"] == knowledge.SCORE_SEARCH_HISTORY
    assert first["relevance"] == knowledge.RELEVANCE_BAIXA


def test_valor_vazio_nao_gera_busca(webapi: Any, fake_router: Any) -> None:
    """ufed:Value em branco: nenhuma busca registrada (:394-395)."""
    _add_doc(
        webapi,
        109,
        {"ufed:Source": ["chrome"], "ufed:Value": ["   "]},
        "web history",
    )

    out = comm.build_triage_communication(SOURCE_ID)

    assert out["searches"] == []
    assert out["message"] == knowledge.COMMUNICATION_EMPTY_MESSAGE


# --- (b)/(c) pontuação pinada (draft_report:866-869) ------------------------------


def test_score_com_termos_min_50_mais_total_95(
    webapi: Any, fake_router: Any
) -> None:
    """Com termos: min(50+total, 95) e ALTA; total = max(chat_total, count)."""
    _add_doc(
        webapi,
        110,
        {
            "Communication:To": ["Maria (+5511)"],
            "Conversation:messagesCount": ["60"],
            "ufed:Body": ["transferencia pix dinheiro"],
        },
        "whatsapp",
    )
    _add_doc(
        webapi,
        111,
        {
            "Communication:To": ["Nadia (+5531)"],
            "Conversation:messagesCount": ["20"],
            "ufed:Body": ["nada haver"],
        },
        "whatsapp",
    )

    out = comm.build_triage_communication(SOURCE_ID, ("dinheiro", "pix"))

    chats = _chats_by_name(out)
    maria = chats["MARIA"]
    assert maria["score"] == 95  # min(50+60, 95)
    assert maria["relevance"] == knowledge.RELEVANCE_ALTA
    assert maria["terms_hits"] == ["dinheiro", "pix"]
    assert out["top_terms"] == {"dinheiro": 1, "pix": 1}
    nadia = chats["NADIA"]
    assert nadia["score"] == 50  # sem hit de termo: min(40+20//2, 70)
    assert nadia["terms_hits"] == []


def test_score_sem_termos_politica_estrita(
    webapi: Any, fake_router: Any
) -> None:
    """Sem termos: min(40+total//2, 70); MÉDIA só com total > 50 (ESTRITO)."""
    _add_doc(
        webapi,
        112,
        {
            "Communication:To": ["Aline (+55)"],
            "Conversation:messagesCount": ["50"],
        },
        "chats",
    )
    _add_doc(
        webapi,
        113,
        {
            "Communication:To": ["Bruno (+55)"],
            "Conversation:messagesCount": ["51"],
        },
        "chats",
    )

    out = comm.build_triage_communication(SOURCE_ID)

    chats = _chats_by_name(out)
    assert chats["ALINE"]["score"] == 65  # 40 + 50//2
    assert chats["ALINE"]["relevance"] == knowledge.RELEVANCE_BAIXA
    assert chats["BRUNO"]["relevance"] == knowledge.RELEVANCE_MEDIA
    assert "top_terms" not in out


def test_termo_no_nome_do_doc_tambem_configura_hit(
    webapi: Any, fake_router: Any
) -> None:
    """Termo case-insensitive no NOME do item (:496-508 usa nome + corpo)."""
    _add_doc(
        webapi,
        114,
        {"Communication:To": ["Iris (+55)"], "ufed:Body": ["ok"]},
        "whatsapp",
        name="PRINT PIX.png",
    )

    out = comm.build_triage_communication(SOURCE_ID, ("pix",))

    assert _chats_by_name(out)["IRIS"]["terms_hits"] == ["pix"]


# --- (d) dedup de queries: builder compartilhado, nunca réplica -------------------


def test_queries_usam_o_builder_whatsapp_compartilhado(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prova do MANDATO DEDUP: patch no builder muda a query emitida.

    Se communication.py replicasse a string, o patch não teria efeito.
    """
    monkeypatch.setattr(
        wa_queries, "build_all_wa_query", lambda: 'category:"whatsapp"'
    )

    queries = comm._communication_queries()

    assert 'category:"whatsapp"' in queries
    assert WA_QUERY not in queries
    # As 12 categorias continuam presentes, sem duplicata alguma:
    assert len(queries) == len(set(queries)) == len(knowledge.COMMUNICATION_CATEGORIES)


def test_query_whatsapp_unica_vinda_do_builder_importado() -> None:
    """Sem patch: exatamente UMA query == builder, zero strings replicadas."""
    queries = comm._communication_queries()

    expected = wa_queries.build_all_wa_query()
    assert queries.count(expected) == 1
    assert len(queries) == len(knowledge.COMMUNICATION_CATEGORIES) + 1
    for query in queries:
        assert query == expected or query.startswith('category:"')


# --- (e) tetos marcados ------------------------------------------------------------


def _add_person(webapi: Any, doc_id: int, name: str) -> None:
    """Registra uma mensagem privada de ``name`` (indivíduo distinto)."""
    _add_doc(
        webapi,
        doc_id,
        {"Communication:To": [f"{name} (+55{doc_id})"], "isGroupMessage": ["false"]},
        "whatsapp",
    )


def test_teto_de_individuos_grupos_sempre_mantidos(
    webapi: Any, fake_router: Any
) -> None:
    """21 indivíduos: visão traz 20; o grupo entra mesmo no fim da ordem."""
    for i in range(21):
        _add_person(webapi, 200 + i, f"Pessoa {i}")
        # ordem por total: mensagens do grupo têm total maior (100)
    _add_doc(
        webapi,
        300,
        {"Communication:To": ["Grupo Top (id:9)@g.us"], "isGroupMessage": ["true"],
         "Conversation:messagesCount": ["100"]},
        "chats",
    )
    _add_doc(
        webapi,
        301,
        {"Communication:To": ["Grupo Vazio (id:8)@g.us"], "isGroupMessage": ["true"]},
        "chats",
    )

    out = comm.build_triage_communication(SOURCE_ID)

    assert out["chats_individuals_total"] == 21
    assert out["chats_grupos_total"] == 2
    names = [chat["interlocutor"] for chat in out["chats"]]
    assert sum(1 for n in names if n.startswith("PESSOA")) == 20
    assert "GRUPO TOP" in names and "GRUPO VAZIO" in names
    assert out["truncated"]["chats_omitted"] == 1


def test_teto_grupo_desconhecido_conta_como_individuo(
    webapi: Any, fake_router: Any
) -> None:
    """GRUPO DESCONHECIDO não pontua como grupo (:735-738)."""
    _add_doc(
        webapi,
        310,
        {"Communication:To": [knowledge.COMMUNICATION_UNKNOWN_GROUP_LABEL],
         "isGroupMessage": ["true"]},
        "chats",
    )

    out = comm.build_triage_communication(SOURCE_ID)

    assert out["chats_grupos_total"] == 0
    assert out["chats_individuals_total"] == 1


def test_teto_da_visao_e_do_historico_marcados(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHATS_VIEW_MAX e SEARCH_HISTORY_MAX cortam com contagem marcada."""
    monkeypatch.setattr(knowledge, "CHAT_INDIVIDUALS_TOP", 2)
    monkeypatch.setattr(knowledge, "CHATS_VIEW_MAX", 2)
    monkeypatch.setattr(knowledge, "SEARCH_HISTORY_MAX", 2)
    for i in range(3):
        _add_person(webapi, 400 + i, f"Gente {i}")
        _add_doc(
            webapi,
            410 + i,
            {"ufed:Source": ["chrome"], "ufed:Value": [f"busca {i}"],
             "ufed:TimeStamp": [f"2026-01-0{i}"]},
            "web history",
        )

    out = comm.build_triage_communication(SOURCE_ID)

    assert len(out["chats"]) == 2 and len(out["searches"]) == 2
    assert out["truncated"]["chats_omitted"] == 1
    assert out["truncated"]["search_history_omitted"] == 1


def test_teto_de_varredura_chat_scan_cap(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CHAT_SCAN_CAP deduplica e corta a varredura, marcado (§8.4)."""
    monkeypatch.setattr(knowledge, "CHAT_SCAN_CAP", 2)
    for i in range(4):
        _add_person(webapi, 500 + i, f"Gente {i}")

    out = comm.build_triage_communication(SOURCE_ID)

    assert out["truncated"]["scan_cap_applied"] is True
    assert out["counters"]["docs_searched"] == 2


# --- (f) contratos M-2 e orçamento R-TRIAGE-1 --------------------------------------


def test_query_500_vira_hit_negativo_e_warning(
    webapi: Any, fake_router: Any
) -> None:
    """500 em uma categoria: -1 + warning nominal; eixo segue (§0.1)."""
    webapi.fail_search_queries.add('category:"chats"')
    _add_person(webapi, 600, "Zoe")

    out = comm.build_triage_communication(SOURCE_ID)

    assert out["query_hits"]['category:"chats"'] == -1
    assert any('category:"chats"' in w and "falhou" in w for w in out["warnings"])
    assert out["query_hits"]['category:"whatsapp"'] == 1


def test_source_inexistente_vira_value_error_estavel(fake_router: Any) -> None:
    """404 de source: ValueError estável ANTES de qualquer HTTP (M-2)."""
    with pytest.raises(ValueError, match="não encontrado em nenhum endpoint"):
        comm.build_triage_communication(UNKNOWN_SOURCE)


def test_eixo_vazio_e_mensagem_explicita_sem_excecao(
    webapi: Any, fake_router: Any
) -> None:
    """Nada de comunicação: message explícita + as 13 queries pagas."""
    out = comm.build_triage_communication(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS | {"message"}
    assert out["message"] == knowledge.COMMUNICATION_EMPTY_MESSAGE
    assert out["counters"]["http_calls"] == len(knowledge.COMMUNICATION_CATEGORIES) + 1


def test_orcamento_estourado_vira_parcial_marcado(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap = queries + 1 doc: parcial não vazio, nunca exceção (§2.3)."""
    cap = len(knowledge.COMMUNICATION_CATEGORIES) + 1 + 1  # 13 queries + 1 doc
    monkeypatch.setitem(knowledge.TOOL_BUDGETS, "triage_communication", cap)
    for i in range(3):
        _add_person(webapi, 700 + i, f"Gente {i}")

    out = comm.build_triage_communication(SOURCE_ID)

    counters, truncated = out["counters"], out["truncated"]
    assert counters["phase"] == "partial"
    assert counters["http_calls"] == cap
    assert truncated["http_budget_exceeded"] is True
    assert truncated["docs_omitted_by_budget"] == 2
    assert any("http_budget_exceeded" in w and f"cap {cap}" in w for w in out["warnings"])
    assert len(out["chats"]) == 1  # parcial NÃO vazio


def test_http_calls_exato_e_igual_ao_trafego_do_fake(
    webapi: Any, fake_router: Any
) -> None:
    """Pino de exatidão: 13 queries + N docs == http_calls == fake."""
    for i in range(3):
        _add_person(webapi, 800 + i, f"Gente {i}")
    webapi.requests_log.clear()

    out = comm.build_triage_communication(SOURCE_ID)

    paths = [entry["path"] for entry in webapi.requests_log]
    searches = paths.count("/search")
    docs = sum("/docs/" in p for p in paths)
    assert searches == len(knowledge.COMMUNICATION_CATEGORIES) + 1 == 13
    assert out["counters"]["http_calls"] == searches + docs == 13 + 3
    assert out["truncated"]["http_budget_exceeded"] is False


def test_query_duplicada_nunca_pagas_duas_vezes(
    webapi: Any, fake_router: Any
) -> None:
    """Categoria que o builder também cobre: query única no orçamento/plano."""
    plan = comm._communication_queries()
    webapi.requests_log.clear()

    comm.build_triage_communication(SOURCE_ID)

    q_values = [
        e["raw_target"] for e in webapi.requests_log if e["path"] == "/search"
    ]
    assert len(plan) == len(set(plan))
    assert len(q_values) == len(plan)  # cada query exatamente UMA vez
