"""T-9 (DESIGN 09-B §9.7): owner_candidates — motor heurístico, sem LLM.

Casos pinados: identidade forte vira candidato ALTA (a); sem identidade,
sinais indiretos de financeiro/mídia/comunicação (b); fusão compacta com
cap acumulativo e renome (c); teto de 3 reforços por família de fonte (d);
penalidade ×0.3 só-financeiro (e); vetos literais (f); confiança 60/35 (g);
teto de 5 candidatos (h); telefonia por paridade (i); orquestração no fake
sem query nova, com http_calls composto, eixo-caído não fatal, 404, vazio e
orçamento (j..o). Backend: FakeWebAPI da suíte; nenhum HTTP real.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from iped_mcp import progress, triage
from iped_mcp.triage import knowledge, owner, tool
from tests.conftest import SOURCE_ID, UNKNOWN_SOURCE, make_doc

ENVELOPE_KEYS = {
    "source_id",
    "candidates",
    "total_candidates",
    "financial_only_penalty_applied",
    "per_axis_available",
    "counters",
    "truncated",
    "warnings",
}

IDENTITY_DOC_QUERY = 'category:"user accounts"'


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
        category: categoria canônica para o matcher category_hits.
    """
    full = dict(props)
    if category is not None:
        full["category"] = [category]
        api.category_hits.setdefault(category, []).append(doc_id)
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, name, full)


def _identity_block(**overrides: Any) -> dict[str, Any]:
    """Envelope T-3 mínimo (bloco ``identity``) para o feed do motor.

    Args:
        overrides: chaves do bloco ``identity`` a definir.

    Returns:
        Envelope no formato §3.7 com o bloco dado.
    """
    return {"identity": dict(overrides)}


def _engine_with(*views: tuple[str, float, str, float]) -> owner._OwnerEngine:
    """Engine com vistas alimentadas direto (raw, base, fonte, cap).

    Args:
        views: tuplas ``(raw_name, base, source, cap)`` na ordem dada.

    Returns:
        Motor já alimentado.
    """
    engine = owner._OwnerEngine()
    for raw, base, source, cap in views:
        engine.add_candidate(raw, base, source, cap)
    return engine


# --- (a) identidade forte ------------------------------------------------------


def test_identidade_forte_vira_candidato_alta() -> None:
    """Nome da conta principal: 70/70, ALTA, fonte literal do engine."""
    engine = owner._OwnerEngine()
    engine.feed_identity(_identity_block(name="Contato Teste Um"))

    out = engine.rank()

    assert out["total"] == 1
    cand = out["candidates"][0]
    assert cand["name"] == "Contato Teste Um"
    assert cand["score"] == 70.0
    assert cand["confidence"] == knowledge.RELEVANCE_ALTA
    assert cand["sources"] == [knowledge.OWNER_SRC_ACCOUNT_MAIN]
    assert cand["evidence"] == (
        "Nome 'Contato Teste Um' eleito com score 70.0. Fontes: "
        "Identidade: conta principal do dispositivo."
    )


def test_sentinela_de_identidade_nao_vira_candidato() -> None:
    """'Não identificado' (sentinela T-3) é vetado — senão venceria com 70."""
    engine = owner._OwnerEngine()
    engine.feed_identity(
        _identity_block(name=knowledge.NOT_IDENTIFIED_NAME)
    )

    assert engine.rank()["total"] == 0


# --- (b) sem identidade: sinais indiretos --------------------------------------


def test_sinais_indiretos_sem_identidade() -> None:
    """Financeiro/mídia/comunicação isolados pontuam com pesos literais."""
    engine = owner._OwnerEngine()
    engine.feed_financial({
        "evidences": [{"payer": "Carlos Pagador", "payee": ""}],
        "cards": [{"description": "Pagamento Ana Souza"}],
        "accounts": [],
    })
    engine.feed_media({"owner_media_signals": ["Beatriz Lima", "ab"]})
    engine.feed_communication({
        "chats": [
            {"interlocutor": "Eduardo Fono", "message_count": 250},
            {"interlocutor": "Fora do Corte", "message_count": 100},
        ]
    })

    scores = {c["name"]: c["score"] for c in engine.rank()["candidates"]}

    assert scores == {
        "Carlos Pagador": 18.0,          # OWNER_W_PAYER :204-206
        "Pagamento Ana Souza": 20.0,     # OWNER_W_CARD_DESC :218-221
        "Beatriz Lima": 10.0,            # OWNER_W_MEDIA :243-252
        "Eduardo Fono": 5.0,             # OWNER_W_INTERLOCUTOR :231-238
    }


# --- (c) fusão compacta ---------------------------------------------------------


def test_fusao_compacta_cap_acumulativo_e_fontes() -> None:
    """"Titular Teste" (70) + "TitularTeste" (55) fundem: 1 candidato, cap 70."""
    engine = _engine_with(
        ("Titular Teste", 70.0, knowledge.OWNER_SRC_ACCOUNT_MAIN, 70.0),
        ("TitularTeste", 55.0, knowledge.OWNER_SRC_SELF_CONTACT, 55.0),
    )

    out = engine.rank()

    assert out["total"] == 1
    cand = out["candidates"][0]
    assert cand["name"] == "Titular Teste"
    assert cand["score"] == 70.0  # min(70+55, max(70,55))
    assert cand["sources"] == [
        knowledge.OWNER_SRC_ACCOUNT_MAIN,
        knowledge.OWNER_SRC_SELF_CONTACT,
    ]


def test_fusao_renomeia_para_nome_com_mais_palavras() -> None:
    """A vista com mais palavras passa a exibir (engine :316-322)."""
    engine = _engine_with(
        ("TitularTeste", 70.0, knowledge.OWNER_SRC_ACCOUNT_MAIN, 70.0),
        ("Titular Teste", 15.0, knowledge.OWNER_SRC_EMAIL_ALIAS, 15.0),
    )

    cand = engine.rank()["candidates"][0]

    assert cand["name"] == "Titular Teste"
    assert cand["score"] == 70.0  # min(70+15, 70)


def test_cap_max_da_familia_de_fontes() -> None:
    """Cap acumulativo = máx entre fontes fundidas (wifi 15 + pagador 20)."""
    engine = _engine_with(
        ("Titular Teste", 12.0, knowledge.OWNER_SRC_WIFI.format(ssid="x"), 15.0),
        ("Titular Teste", 18.0, knowledge.OWNER_SRC_PAYER, 20.0),
    )

    cand = engine.rank()["candidates"][0]

    assert cand["score"] == 20.0  # min(12+18, max(15,20))


# --- (d) teto de reforço por família --------------------------------------------


def test_reforco_de_familia_para_na_terceira_fonte() -> None:
    """4ª fonte da mesma família soma fonte mas não soma score (:310-312)."""
    engine = _engine_with(
        ("Mario Compras", 10.0, "Financeiro: pagador em transação", 100.0),
        ("Mario Compras", 10.0, "Financeiro: recebedor", 100.0),
        ("Mario Compras", 10.0, "Financeiro: favorecido em comprovante", 100.0),
        ("Mario Compras", 10.0, "Financeiro: descrição de cartão", 100.0),
    )

    cand = engine.rank()["candidates"][0]

    assert cand["score"] == 30.0  # 3 reforços; a 4ª só registra fonte
    assert len(cand["sources"]) == 3  # fontes view[:3]


# --- (e) penalidade finance-only -------------------------------------------------


def test_penalidade_x03_para_vencedor_so_financeiro() -> None:
    """Vencedor só-Financeiro com score>100: ×0.3, re-rank, flag no rank."""
    engine = _engine_with(
        ("Fulano Da Silva", 105.0, "Financeiro: pagador em transação", 200.0),
        ("Contato Teste Um", 70.0, knowledge.OWNER_SRC_ACCOUNT_MAIN, 70.0),
    )

    out = engine.rank()

    assert out["penalty"] == {
        "applied": True, "name": "Fulano Da Silva",
    }
    assert out["candidates"][0]["name"] == "Contato Teste Um"
    assert out["candidates"][1]["score"] == 31.5  # 105 * 0.3
    assert out["candidates"][1]["confidence"] == knowledge.RELEVANCE_BAIXA


def test_sem_penalidade_com_qualquer_fonte_nao_financeira() -> None:
    """Uma única fonte fora de "Financeiro" já desarma a penalidade."""
    engine = _engine_with(
        ("Fulano Da Silva", 105.0, "Financeiro: pagador em transação", 200.0),
        ("Fulano Da Silva", 105.0, "Mídias: titular agregado", 200.0),
    )

    out = engine.rank()

    assert out["penalty"]["applied"] is False
    assert out["candidates"][0]["score"] == 200.0  # cap


# --- (f) vetos literais -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Contato Teste Um.",            # regra absoluta do ponto (:285-289)
        "arquivo_indexado.pdf",      # técnico: ponto sem espaço/underscore
        "com.android.phone",         # pacote reverse-domain
        "Banco Bradesco",            # institucional (word-level)
        "Cartório de Paz",           # institucional acentuado (folded)
        "FAPLMC0000148",             # ID de transação (padrão literal)
        "WhatsApp Backup",           # não-pessoa (app + admin)
        "1234567890123",             # só dígitos
    ],
)
def test_vetos_de_candidato(raw: str) -> None:
    """Cada rejeição espelha o veto correspondente do engine."""
    engine = _engine_with(
        (raw, 70.0, knowledge.OWNER_SRC_ACCOUNT_MAIN, 70.0)
    )

    assert engine.rank()["total"] == 0


def test_uppercase_vira_title_case_e_digitos_vetam() -> None:
    """Normalização :383 (todo maiúsculo → título) e dígito → veto (:428)."""
    engine = owner._OwnerEngine()

    assert engine.normalize_name("Titular Teste") == "Titular Teste"
    assert engine.looks_like_name("Titular Te2te") is False
    assert engine.is_transaction_id("V-3022158736541") is True
    # sigla pura: o próprio upper→title do normalize a converte antes do
    # veto (quirk fiel do engine) — o veto de sigla vale na forma crua:
    assert engine.is_institutional("ABCDE") is True


# --- (g) confiança 60/35 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (60.0, knowledge.RELEVANCE_ALTA),   # borda inferior ALTA (:450)
        (59.9, knowledge.RELEVANCE_MEDIA),
        (35.0, knowledge.RELEVANCE_MEDIA),  # borda inferior MÉDIA (:452)
        (34.9, knowledge.RELEVANCE_BAIXA),
    ],
)
def test_confianca_bordas_60_e_35(score: float, expected: str) -> None:
    """Rótulos RELEVANCE_* nos limiares literais do engine (:449-454)."""
    assert owner._OwnerEngine.score_to_confidence(score) == expected


# --- (h) teto de 5 candidatos ------------------------------------------------------


def test_teto_de_cinco_com_contagem_pre_teto() -> None:
    """7 candidatos: devolve 5 ranqueados, total pré-teto 7 (§9.5 :127-130)."""
    engine = owner._OwnerEngine()
    for name in ("Ana Beta", "Caio Delta", "Eva Sigma", "Ivo Ômega",
                 "Lia Teta", "Nuno Zeta", "Otto Iota"):
        engine.add_candidate(
            name, 50.0, "Path do documento: 'x - %s'" % name, 60.0
        )

    out = engine.rank()

    assert out["total"] == 7
    assert len(out["candidates"]) == 5


def test_desempate_mantem_ordem_de_insercao() -> None:
    """Empate de score: ordem de inserção (sorted estável do engine :110)."""
    engine = owner._OwnerEngine()
    for name in ("Ana Beta", "Caio Delta"):
        engine.add_candidate(
            name, 20.0, "Financeiro: descrição de cartão", 20.0
        )

    names = [c["name"] for c in engine.rank()["candidates"]]

    assert names == ["Ana Beta", "Caio Delta"]


# --- (i) telefonia e Wi-Fi (paridade com o engine) ---------------------------------


def test_telefonia_paridade_limiar_e_sentinela() -> None:
    """Contato recorrente: 8/10 só com >20 chamadas e nome != DESCONHECIDO."""
    engine = owner._OwnerEngine()
    engine.feed_telephony({
        "top_contacts": [
            {"contact_name": "Contato Recorrente", "total_calls": 25},
            {"contact_name": "No Limite", "total_calls": 20},
            {
                "contact_name": knowledge.DEFAULT_CONTACT_NAME,
                "total_calls": 500,
            },
        ]
    })

    out = engine.rank()

    assert out["total"] == 1
    cand = out["candidates"][0]
    assert (cand["name"], cand["score"]) == ("Contato Recorrente", 8.0)
    assert cand["sources"] == [knowledge.OWNER_SRC_TELEPHONY]


def test_wifi_generico_e_hex_fora_pessoal_dentro() -> None:
    """SSID pessoal 12/15; genérico (set) e hex de BSSID vetados (:441-447)."""
    engine = owner._OwnerEngine()
    engine.feed_identity(_identity_block(
        wifi_networks=["Vivo", "a1:b2:c3:d4:e5:f6", "Titular Vivo"],
    ))

    names = {c["name"] for c in engine.rank()["candidates"]}

    assert names == {"Titular Vivo"}


def test_self_contact_e_alias_de_email() -> None:
    """55 via amostra com o próprio número; 15 via local-part do e-mail."""
    engine = owner._OwnerEngine()
    engine.feed_identity(_identity_block(
        sim_msisdn="5511999990017",
        known_contacts_sample=[
            {"phone": "+55 11 99999-0017", "name": "Contato Teste Um"},
            {"phone": "5511999990014", "name": "Outro Contato"},
            {"phone": "5511999990017",
             "name": knowledge.DEFAULT_CONTACT_NAME},
        ],
        emails=["titular.teste@pf.example.invalid", "sem-arroba"],
    ))

    scores = {c["name"]: c["score"] for c in engine.rank()["candidates"]}

    # "Linha 7": quirk FIEL do engine — o feed SIM injeta "Linha: <msisdn>"
    # cru e o strip de telefone deixa resíduo (verificado byte-a-byte contra
    # owner_inference_engine._normalize_name); score 5 nunca decide sozinho.
    assert scores == {
        "Contato Teste Um": 55.0, "Titular Teste": 15.0, "Linha 7": 5.0,
    }


def test_feed_de_path_para_no_primeiro_aceito() -> None:
    """60/60 no 1º segmento válido; modelo de aparelho e path simples fora."""
    engine = owner._OwnerEngine()
    engine.feed_paths([
        "E:/EVIDENCIAS/UFED/analise.pdf",           # sem " - "
        "E:/Samsung Galaxy S23 - UFED/cel.pdf",     # modelo vetado
        "E:/CASO - Titular Teste/UFED/x.pdf",         # aceito → para aqui
        "E:/OUTRO - NOME QUE NAO ENTRARA/y.pdf",
    ])

    out = engine.rank()

    assert out["total"] == 1
    cand = out["candidates"][0]
    assert (cand["name"], cand["score"]) == ("Titular Teste", 60.0)
    assert cand["sources"] == [
        "Path do documento: 'Titular Teste'"
    ]


# --- (j..o) orquestração no fake --------------------------------------------------


def _seed_owner_case(api: Any) -> None:
    """Identidade ALTA + reconhecido com path de caso (feed de path).

    Args:
        api: estado observável do FakeWebAPI.
    """
    _add_doc(
        api, 901, "account.json",
        {
            "ufed:Source": ["google"],
            "ufed:Name": ["Contato Teste Um"],
            "ufed:EmailAddress": ["titular.teste@gmail.example.invalid"],
        },
        category="user accounts",
    )
    _add_doc(
        api, 902, "rec.ufdr",
        {"ufed:DeviceModel": ["Pixel 7"],
         "path": ["E:/CASO - Titular Teste/UFED/rec.ufdr"]},
        category="recognized devices",
    )


def _member_targets_and_counts(api: Any) -> tuple[set[str], dict[str, int]]:
    """Trafego (raw_target) e http_calls de cada membro rodando sozinho.

    Args:
        api: estado observável do FakeWebAPI.

    Returns:
        ``(união dos raw_targets, {axis: http_calls})``.
    """
    builds = {
        "identity": owner.build_triage_identity,
        "financial": owner.build_triage_financial,
        "media": owner.build_triage_media,
        "communication": owner.build_triage_communication,
    }
    targets: set[str] = set()
    counts: dict[str, int] = {}
    for axis, build in builds.items():
        api.requests_log.clear()
        envelope = build(SOURCE_ID)
        targets.update(e["raw_target"] for e in api.requests_log)
        counts[axis] = envelope["counters"]["http_calls"]
    return targets, counts


def test_orcamento_composto_e_pin_de_18300() -> None:
    """OWNER_BUDGET derivado da soma dos tetos-membro (§2.2, sem HTTP próprio)."""
    expected = sum(
        knowledge.TOOL_BUDGETS[tool] for tool in knowledge.OWNER_MEMBER_TOOLS
    )
    assert knowledge.OWNER_BUDGET == 18300 == expected


def test_envelope_e_candidatos_do_caso_semeado(
    webapi: Any, fake_router: Any
) -> None:
    """Identidade forte (70, ALTA) + fusão alias/path (60, ALTA), shape §9.6."""
    _seed_owner_case(webapi)

    out = tool.owner_candidates(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS
    assert out["per_axis_available"] == {
        "identity": True, "financial": True, "media": True,
        "communication": True,
    }
    assert out["total_candidates"] == 2
    top = out["candidates"][0]
    assert (top["name"], top["score"], top["confidence"]) == (
        "Contato Teste Um", 70.0, knowledge.RELEVANCE_ALTA,
    )
    second = out["candidates"][1]
    assert (second["name"], second["score"]) == ("Titular Teste", 60.0)
    assert second["sources"] == [
        knowledge.OWNER_SRC_EMAIL_ALIAS,
        "Path do documento: 'Titular Teste'",
    ]
    assert out["financial_only_penalty_applied"] == {
        "applied": False, "name": None,
    }
    assert out["truncated"] == {
        "http_budget_exceeded": False,
        "http_budget_calls_at_stop": None,
        "members": {"identity": False, "financial": False, "media": False,
                    "communication": False},
        "candidates_truncated": False,
    }


def test_http_calls_composto_soma_dos_membros(
    webapi: Any, fake_router: Any
) -> None:
    """http_calls == soma dos contadores-membro; nenhum GET fora dos eixos."""
    _seed_owner_case(webapi)
    _member_targets_and_counts(webapi)  # esquenta cache e serve de pino

    webapi.requests_log.clear()
    out = tool.owner_candidates(SOURCE_ID)

    member = out["counters"]["member_http_calls"]
    assert set(member) == {"identity", "financial", "media", "communication"}
    assert out["counters"]["http_calls"] == sum(member.values()) > 0
    assert out["counters"]["http_budget"] == knowledge.OWNER_BUDGET
    paths = [e["path"] for e in webapi.requests_log]
    # forma real do jar: /search e /sources/<sid>/docs/<id> (mesma checagem
    # do pino de identidade, som("/docs/" in p))
    assert all(p == "/search" or "/docs/" in p for p in paths)
    assert out["counters"]["cached"] is False


def test_nenhuma_query_nova_alem_dos_eixos_membros(
    webapi: Any, fake_router: Any
) -> None:
    """Todo GET do owner_candidates é um GET que um membro já emite (§9.1)."""
    _seed_owner_case(webapi)
    member_targets, member_counts = _member_targets_and_counts(webapi)

    webapi.requests_log.clear()
    out = tool.owner_candidates(SOURCE_ID)

    owner_targets = {e["raw_target"] for e in webapi.requests_log}
    assert owner_targets <= member_targets
    for axis, count in out["counters"]["member_http_calls"].items():
        assert count <= member_counts[axis]  # cache compartilhado só reduz


def test_eixo_que_falha_nao_derruba_a_ferramenta(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Comunicação explodes: warning + per_axis_available false; demais seguem."""
    _seed_owner_case(webapi)

    def _boom(_sid: str) -> dict[str, Any]:
        raise RuntimeError("eixo artificialmente quebrado")

    monkeypatch.setattr(owner, "build_triage_communication", _boom)
    out = tool.owner_candidates(SOURCE_ID)

    assert out["per_axis_available"]["communication"] is False
    assert out["per_axis_available"]["identity"] is True
    assert any(
        w.startswith("eixo communication indisponível")
        for w in out["warnings"]
    )
    assert out["candidates"][0]["name"] == "Contato Teste Um"


def test_orcamento_estourado_vira_parcial_marcado(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Composto acima do cap: truncated + warning + phase partial (§9.6)."""
    _seed_owner_case(webapi)
    monkeypatch.setattr(knowledge, "OWNER_BUDGET", 5)

    out = tool.owner_candidates(SOURCE_ID)

    assert out["truncated"]["http_budget_exceeded"] is True
    assert out["truncated"]["http_budget_calls_at_stop"] == (
        out["counters"]["http_calls"]
    )
    assert out["counters"]["phase"] == progress.PHASE_PARTIAL
    assert any(
        "orçamento composto estourado" in w for w in out["warnings"]
    )


def test_source_inexistente_vira_value_error_estavel(fake_router: Any) -> None:
    """404 de source: ValueError estável ANTES de qualquer HTTP (M-2)."""
    with pytest.raises(ValueError, match="não encontrado em nenhum endpoint"):
        tool.owner_candidates(UNKNOWN_SOURCE)


def test_vazio_explicito_sem_candidatos_sem_excecao(
    webapi: Any, fake_router: Any
) -> None:
    """Nada nos eixos: candidates [], message explícita, sem exceção (§0.1)."""
    webapi.docs.clear()

    out = tool.owner_candidates(SOURCE_ID)

    assert out["candidates"] == []
    assert out["total_candidates"] == 0
    assert out["message"] == knowledge.OWNER_EMPTY_MESSAGE
    assert all(out["per_axis_available"].values())


def test_tool_registrada_via_mcp(fake_router: Any, webapi: Any) -> None:
    """owner_candidates é descoberta e chamada pelo MCP (mesmo mecanismo)."""
    from fastmcp import Client, FastMCP

    server = FastMCP("iped_mcp_test")
    triage.register(server)

    async def _run() -> dict[str, Any]:
        async with Client(server) as client:
            names = [t.name for t in await client.list_tools()]
            assert "owner_candidates" in names
            result = await client.call_tool(
                "owner_candidates", {"source_id": SOURCE_ID}
            )
            return (
                result.data
                if result.data is not None
                else json.loads(result.content[0].text)
            )

    payload = asyncio.run(_run())
    assert payload["source_id"] == SOURCE_ID
