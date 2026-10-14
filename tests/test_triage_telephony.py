"""T-1 (DESIGN 09 §2/§5): triage_telephony — top contatos por peso de chamadas.

Casos pinados do design: fluxo feliz com renomeio por contato de agenda e
duração multi-formato (T1-1), limiares ALTA/MÉDIA/BAIXA e teto do score
pinados (T1-2), teto TOP_CONTACTS com truncamento marcado (T1-3), caps de
varredura (T1-4), source inexistente via política M-2 (T1-5), caso vazio
explícito sem exceção (T1-6), query 500 por categoria vira warning (T1-7)
e número sem condições de registro é ignorado (T1-8). Backend: FakeWebAPI
(da suíte) com docs/category_hits adicionados por teste (padrão dos testes
de censo/M-7) — catálogo default intocado.
"""

from __future__ import annotations

from typing import Any

import pytest

from iped_mcp import progress
from iped_mcp.triage import knowledge, telephony, tool
from tests.conftest import SOURCE_ID, UNKNOWN_SOURCE, make_doc

CONTACT_ROW_KEYS = {
    "phone",
    "contact_name",
    "total_calls",
    "calls_out",
    "calls_in",
    "calls_missed",
    "total_duration_seconds",
    "score",
    "relevance",
    "justification",
    "sample_doc_ids",
}
ENVELOPE_KEYS = {
    "source_id",
    "contacts",
    "query_hits",
    "counters",
    "truncated",
    "warnings",
}
CALLS_QUERY = 'category:"calls"'
CONTACTS_QUERY = 'category:"contatos"'


def _add_call(
    api: Any,
    doc_id: int,
    to: str,
    direction: str = "",
    duration: str = "",
    category: str = "calls",
) -> None:
    """Registra doc de chamada no fake (props no formato do jar: lista).

    Args:
        api: estado observável do FakeWebAPI.
        doc_id: id do documento.
        to: valor de Communication:To.
        direction: valor de ufed:Direction ("" = sem informação).
        duration: valor de ufed:Duration ("" = sem informação).
        category: categoria do doc (casa com category_hits).
    """
    api.docs[(SOURCE_ID, doc_id)] = make_doc(
        doc_id,
        f"call-{doc_id}.db",
        {
            "category": [category],
            "Communication:To": [to],
            "ufed:Direction": [direction],
            "ufed:Duration": [duration],
        },
    )
    api.category_hits.setdefault(category, []).append(doc_id)


def _add_agenda_contact(
    api: Any, doc_id: int, name: str, phone: str, category: str = "contatos"
) -> None:
    """Registra doc de contato de agenda no fake (ufed:Name/PhoneNumber).

    Args:
        api: estado observável do FakeWebAPI.
        doc_id: id do documento.
        name: valor de ufed:Name.
        phone: valor cru de ufed:PhoneNumber (dígitos são limpos pelo tool).
        category: categoria do doc.
    """
    api.docs[(SOURCE_ID, doc_id)] = make_doc(
        doc_id,
        f"contacts-{doc_id}.vcf",
        {
            "category": [category],
            "ufed:Name": [name],
            "ufed:PhoneNumber": [phone],
        },
    )
    api.category_hits.setdefault(category, []).append(doc_id)


def _add_call_group(api: Any, start_id: int, phone: str, count: int) -> None:
    """Cria `count` docs de chamada para o MESMO número (limiar de calls).

    Args:
        api: estado observável do FakeWebAPI.
        start_id: primeiro doc id do grupo.
        phone: número-alvo (entra como "(<phone>)" no To).
        count: quantidade de chamadas.
    """
    for i in range(count):
        _add_call(api, start_id + i, to=f"({phone})")


# --- T1-1: fluxo feliz -------------------------------------------------------


def test_fluxo_feliz_agregacao_formatacao_e_ordem(
    webapi: Any, fake_router: Any
) -> None:
    """Shape exato, duração HH:MM:SS/MM:SS/segundos e renomeio pela agenda."""
    _add_call(webapi, 101, to="(5511999998888)", direction="outgoing", duration="00:01:30")
    _add_call(webapi, 102, to="(5511999998888)", direction="missed", duration="02:05")
    _add_call(webapi, 103, to="Maria (+5511999990013)", direction="incoming", duration="60")
    _add_agenda_contact(webapi, 201, "Jose da Silva", "(55 11) 99999-8888")

    out = tool.triage_telephony(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS
    assert "message" not in out
    jose, maria = out["contacts"]  # BAIXA empatada -> ordem por -score (52, 51)
    assert set(jose) == CONTACT_ROW_KEYS
    assert jose == {
        "phone": "5511999998888",
        "contact_name": "JOSE DA SILVA",
        "total_calls": 2,
        "calls_out": 1,
        "calls_in": 0,
        "calls_missed": 1,
        "total_duration_seconds": 90 + 125,
        "score": 52,
        "relevance": "BAIXA",
        "justification": "2 chamadas (E:1/R:0/P:1)",
        "sample_doc_ids": [101, 102, 201],
    }
    assert maria["contact_name"] == "MARIA"
    assert maria["phone"] == "+5511999990013"  # '+' preservado (quirk do ICARO)
    assert maria["total_duration_seconds"] == 60
    assert out["query_hits"] == {
        'category:"chamadas telefônicas"': 0,
        CALLS_QUERY: 3,
        'category:"call logs"': 0,
        CONTACTS_QUERY: 1,
        'category:"contacts"': 0,
    }
    counters = out["counters"]
    assert counters["phase"] == progress.PHASE_COMPLETE
    assert counters["scanned"] == 4 and counters["processed"] == 4
    assert (
        counters["docs_searched"],
        counters["docs_fetched"],
        counters["call_docs"],
        counters["contact_docs"],
        counters["contacts_total"],
        counters["cached"],
    ) == (4, 4, 3, 1, 2, False)
    assert out["truncated"] == {
        "total_cap_applied": False,
        "query_caps_applied": [],
        "contacts_truncated": False,
        "contacts_omitted": 0,
        "http_budget_exceeded": False,
        "http_budget_calls_at_stop": None,
        "docs_omitted_by_budget": 0,
    }
    assert out["warnings"] == []


# --- T1-2: limiares e teto do score PINADOS ----------------------------------


def test_limiares_de_relevancia_e_teto_de_score(
    webapi: Any, fake_router: Any
) -> None:
    """31→ALTA, 30→MÉDIA, 11→MÉDIA, 10→BAIXA; score=min(50+n,90) em n=100."""
    groups = {  # phone -> total_calls (limiares ESTRITOS do draft_report:915)
        "5511999990001": 31,
        "5511999990002": 30,
        "5511999990019": 11,
        "5511999990015": 10,
        "5511999990016": 100,
    }
    next_id = 1000
    for phone, count in groups.items():
        _add_call_group(webapi, next_id, phone, count)
        next_id += count

    contacts = tool.triage_telephony(SOURCE_ID)["contacts"]

    by_phone = {c["phone"]: c for c in contacts}
    assert [(c["phone"], c["relevance"], c["score"]) for c in contacts] == [
        ("5511999990016", "ALTA", 90),  # 100 chamadas: cap 90 do min(50+n,90)
        ("5511999990001", "ALTA", 81),  # 31 > 30
        ("5511999990002", "MÉDIA", 80),  # 30 NÃO é ALTA (limiar estrito)
        ("5511999990019", "MÉDIA", 61),  # 11 > 10
        ("5511999990015", "BAIXA", 60),  # 10 NÃO é MÉDIA (limiar estrito)
    ]
    assert by_phone["5511999990001"]["justification"] == "31 chamadas (E:0/R:0/P:0)"


# --- T1-3: teto TOP_CONTACTS sempre marcado ----------------------------------


def test_teto_top20_marca_truncamento(webapi: Any, fake_router: Any) -> None:
    """25 números -> 20 entregues; contacts_truncated + contacts_omitted=5."""
    for i in range(25):
        _add_call(webapi, 300 + i, to=f"(5577{i:08d})")

    out = tool.triage_telephony(SOURCE_ID)

    assert len(out["contacts"]) == knowledge.TOP_CONTACTS == 20
    assert out["truncated"]["contacts_truncated"] is True
    assert out["truncated"]["contacts_omitted"] == 5
    assert out["counters"]["contacts_total"] == 25


# --- T1-4: caps de varredura ---------------------------------------------------


def test_cap_total_de_candidatos_aplicado_e_marcado(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOTAL_CANDIDATE_CAP pequeno limita a varredura e marca o truncamento."""
    monkeypatch.setattr(knowledge, "TOTAL_CANDIDATE_CAP", 3)
    for i in range(8):
        _add_call(webapi, 400 + i, to=f"(5566{i:08d})")

    out = tool.triage_telephony(SOURCE_ID)

    assert out["truncated"]["total_cap_applied"] is True
    assert out["counters"]["docs_searched"] == 3


def test_cap_por_query_nomeia_a_query_exata(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CATEGORY_QUERY_CAP estourado lista a query exata em query_caps_applied."""
    monkeypatch.setattr(knowledge, "CATEGORY_QUERY_CAP", 2)
    for i in range(5):
        _add_call(webapi, 500 + i, to=f"(5555{i:08d})")

    out = tool.triage_telephony(SOURCE_ID)

    assert out["truncated"]["query_caps_applied"] == [CALLS_QUERY]
    assert out["query_hits"][CALLS_QUERY] == 5  # hits brutos, antes do cap
    assert out["counters"]["docs_searched"] == 2


# --- T1-5: source inexistente (política M-2) ----------------------------------


def test_source_inexistente_vira_value_error_estavel(
    fake_router: Any
) -> None:
    """ValueError com o texto estável do Router.resolve, antes do HTTP."""
    with pytest.raises(ValueError, match="não encontrado em nenhum endpoint"):
        tool.triage_telephony(UNKNOWN_SOURCE)


# --- T1-6: caso sem telefonia = triagem vazia explícita -----------------------


def test_caso_sem_telefonia_e_vazio_explicito(
    webapi: Any, fake_router: Any
) -> None:
    """Nenhum hit nas 5 categorias: contacts=[], contadores 0, message."""
    out = tool.triage_telephony(SOURCE_ID)

    assert out["contacts"] == []
    assert out["message"] == knowledge.TELEPHONY_EMPTY_MESSAGE
    assert out["counters"]["docs_searched"] == 0
    assert out["counters"]["contacts_total"] == 0
    assert out["counters"]["phase"] == progress.PHASE_COMPLETE
    assert all(hits == 0 for hits in out["query_hits"].values())


# --- T1-7: query 500 em uma categoria = warning, eixo segue -------------------


def test_query_que_falha_vira_warning_e_eixo_segue(
    webapi: Any, fake_router: Any
) -> None:
    """500 em category:"calls": warning na resposta; contatos seguem."""
    webapi.fail_search_queries = {CALLS_QUERY}
    _add_call(webapi, 601, to="(5511999990018)")
    _add_agenda_contact(webapi, 602, "Ana", "551199999012")

    out = tool.triage_telephony(SOURCE_ID)

    assert out["query_hits"][CALLS_QUERY] == -1
    assert any(CALLS_QUERY in w for w in out["warnings"])
    assert [c["phone"] for c in out["contacts"]] == ["551199999012"]
    assert out["contacts"][0]["total_calls"] == 0  # agenda sem chamada
    assert out["contacts"][0]["score"] == knowledge.RELEVANCE_BASE


# --- T1-8: chamada sem número aproveitável é ignorada -------------------------


def test_chamada_sem_numero_util_e_ignorada(
    webapi: Any, fake_router: Any
) -> None:
    """To sem dígitos (>=8) e PhoneNumber curto (<3): doc só em docs_fetched."""
    _add_call(webapi, 701, to="sem-numero-aqui")  # sem parênteses nem dígitos
    _add_call(webapi, 702, to="")
    webapi.docs[(SOURCE_ID, 702)]["properties"]["ufed:PhoneNumber"] = ["12"]
    del webapi.docs[(SOURCE_ID, 702)]["properties"]["Communication:To"]

    out = tool.triage_telephony(SOURCE_ID)

    assert out["contacts"] == []
    assert out["message"] == knowledge.TELEPHONY_EMPTY_MESSAGE
    assert out["counters"]["docs_fetched"] == 2
    assert out["counters"]["call_docs"] == 0


# --- T-1.1 (DESIGN 09-B §1.5): whitelist de nome de contato ------------------


def _add_named_contact(
    api: Any,
    doc_id: int,
    name_field: str | None,
    name: str,
    phone: str = "5511999990010",
    category: str = "contatos",
) -> None:
    """Contato de agenda com o nome em campo ARBITRÁRIO da whitelist T-1.1.

    Args:
        api: estado observável do FakeWebAPI.
        doc_id: id do documento.
        name_field: chave onde gravar o nome (None = nenhum campo de nome).
        name: valor do nome.
        phone: valor de ufed:PhoneNumber.
        category: categoria do doc.
    """
    props: dict[str, Any] = {"category": [category], "ufed:PhoneNumber": [phone]}
    if name_field:
        props[name_field] = [name]
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, f"contacts-{doc_id}.vcf", props)
    api.category_hits.setdefault(category, []).append(doc_id)


def _http_paths(api: Any) -> list[str]:
    """Paths (sem query) de todos os pedidos recebidos pelo fake."""
    return [entry["path"] for entry in api.requests_log]


@pytest.mark.parametrize(
    "name_field",
    ["ufed:Name", "ufed:name", "userName", "ufed:userName"],
)
def test_t11_cada_variante_da_whitelist_produz_nome(
    webapi: Any, fake_router: Any, name_field: str
) -> None:
    """(a) Cada um dos 4 campos, isolado, extrai o nome do contato."""
    _add_named_contact(webapi, 231, name_field, "Ana Pe")

    out = tool.triage_telephony(SOURCE_ID)

    contact = out["contacts"][0]
    assert contact["contact_name"] == "ANA PE"
    assert out["counters"]["contact_docs"] == 1
    assert out["counters"]["contact_name_field_hits"][name_field] == 1


def test_t11_ordem_ufed_name_vence_userName(webapi: Any, fake_router: Any) -> None:
    """(b) ufed:Name E userName no mesmo doc → ufed:Name vence (ordem)."""
    _add_named_contact(webapi, 232, "ufed:Name", "Vencedor")
    webapi.docs[(SOURCE_ID, 232)]["properties"]["userName"] = ["Perdedor"]

    out = tool.triage_telephony(SOURCE_ID)

    assert out["contacts"][0]["contact_name"] == "VENCEDOR"
    hits = out["counters"]["contact_name_field_hits"]
    assert hits["ufed:Name"] == 1 and hits["userName"] == 0


def test_t11_sem_campo_de_nome_interlocutor_desconhecido(
    webapi: Any, fake_router: Any
) -> None:
    """(c) Doc de contato sem NENHUM campo da whitelist: sem registro novo e
    o número atendido por chamada fecha com o DESCONHECIDO explícito
    (behavior atual preservado, §1.5c)."""
    _add_named_contact(webapi, 233, None, "", phone="5511999990012")
    _add_call(webapi, 234, to="(5511999990012)")

    out = tool.triage_telephony(SOURCE_ID)

    rows = {row["phone"]: row for row in out["contacts"]}
    assert rows["5511999990012"]["contact_name"] == "DESCONHECIDO"
    assert out["counters"]["contact_docs"] == 0
    hits = out["counters"]["contact_name_field_hits"]
    assert set(hits) == set(knowledge.CONTACT_NAME_FIELDS)
    assert sum(hits.values()) == 0


def test_t11_histograma_e_aviso_de_origem(webapi: Any, fake_router: Any) -> None:
    """(d) Histograma confere com os docs do fake; ufed:Name=0 + userName>0 →
    aviso 'nome_da_agenda_origem=userName' (elimina o 0-silencioso, §1.3)."""
    _add_named_contact(webapi, 241, "userName", "Casa", phone="5511999990004")
    _add_named_contact(webapi, 242, "userName", "Trabalho", phone="5511999990006")

    out = tool.triage_telephony(SOURCE_ID)

    assert out["counters"]["contact_name_field_hits"] == {
        "ufed:Name": 0,
        "ufed:name": 0,
        "userName": 2,
        "ufed:userName": 0,
    }
    assert any("nome_da_agenda_origem=userName" in w for w in out["warnings"])
    # origem clássica NÃO dispara o aviso
    webapi.requests_log.clear()
    _add_named_contact(webapi, 243, "ufed:Name", "Clássico", phone="5511999990007")
    out2 = tool.triage_telephony(SOURCE_ID)
    assert not any("nome_da_agenda_origem" in w for w in out2["warnings"])


def test_t11_contagem_de_requisicoes_inalterada(
    webapi: Any, fake_router: Any
) -> None:
    """(e) Não-regressão de orçamento: a whitelist NÃO muda o nº de /search
    (1 por categoria canônica) nem de /docs (1 por id único varrido)."""
    _add_named_contact(webapi, 251, "userName", "Ana", phone="5511999990008")
    _add_call(webapi, 252, to="(5511999990009)")

    tool.triage_telephony(SOURCE_ID)

    paths = _http_paths(webapi)
    assert paths.count("/search") == len(knowledge.TELEPHONY_CATEGORIES)
    # GET de doc: sources/<sid>/docs/<id> (client.py:112)
    assert sum("/docs/" in p for p in paths) == 2
