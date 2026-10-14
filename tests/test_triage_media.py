"""T-6 (DESIGN 09-B §6.7): triage_media — itens ranqueados por sinais.

Casos pinados: happy path com soma de sinais (a), cada um dos 12
preditores isolado com seus pontos (b), base 40 chat vs 50 mídia (c),
quirks fiéis — classeNudez string BR, ``_true`` só casa "true" (D-11 do
relatório), "registrato" literal (d), relevância INCLUSIVA 85/60 do handler
:965 (e), histograma de categoria crua/"N/I" + apps + titulares com "N/I"
ignorado e teto 20 (f), histograma de campos com a poda MEDIA_DROP — drop
de chaves de blob (g), teto 50 marcado com total pré-teto (h), warning de
cobertura GPS 0 (i), contratos M-2 (500 por query, 404, vazio) e orçamento
R-TRIAGE-1 com http_calls exato (j). Backend: FakeWebAPI; nenhum HTTP real.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from iped_mcp import progress
from iped_mcp.triage import knowledge, media, tool
from tests.conftest import SOURCE_ID, UNKNOWN_SOURCE, make_doc

ENVELOPE_KEYS = {
    "source_id",
    "ranked_items",
    "items_total",
    "itens_chats",
    "itens_midias",
    "by_category",
    "apps",
    "owner_media_signals",
    "query_hits",
    "counters",
    "truncated",
    "warnings",
}
IMAGE_QUERY = 'category:"image"'
BLOB_KEYS = ("face_encodings", "thumb", "ufed:base64data")


def _add_media_doc(
    api: Any,
    doc_id: int,
    props: dict[str, Any],
    category: str = "image",
    name: str = "m.jpg",
    path: str = "",
    props_category: str | None = None,
) -> None:
    """Registra doc de mídia no fake sob a categoria dada (query canônica).

    Args:
        api: estado observável do FakeWebAPI.
        doc_id: id do documento.
        props: propriedades (valores em lista, formato do jar).
        category: categoria do doc (alimenta ``category:"<cat>"`` e o gate).
        name: nome do arquivo.
        path: propriedade ``path`` (marcador de app/agregados).
    """
    full = dict(props)
    # props_category permite props divergentes do índice (caso real: query
    # casa pelo índice invertido, props vêm de /docs e podem vir vazias).
    full["category"] = [category if props_category is None else props_category]
    if path:
        full["path"] = [path]
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, name, full)
    api.category_hits.setdefault(category, []).append(doc_id)


def _only_item(out: dict[str, Any]) -> dict[str, Any]:
    """Item único de envelopes de um doc (legibilidade dos asserts)."""
    assert out["items_total"] == 1, out
    return out["ranked_items"][0]


# --- (a) happy path -------------------------------------------------------------


def test_fluxo_feliz_soma_de_sinais_e_ordem(webapi: Any, fake_router: Any) -> None:
    """OCR+rosto (50+15+10=75) e doc sem sinal (50): ordem, score e shape."""
    _add_media_doc(webapi, 10, {"ocrCharCount": ["12"], "face_count": ["3"]})
    _add_media_doc(webapi, 11, {})

    out = tool.triage_media(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS
    assert [i["score"] for i in out["ranked_items"]] == [75, 50]
    item = out["ranked_items"][0]
    assert item["signals"] == ["OCR", "rosto"]
    assert item["justification"] == "Mídia: OCR, rosto"
    assert item["relevance"] == knowledge.RELEVANCE_MEDIA  # 75 >= 60
    assert (item["doc_id"], item["name"], item["category"]) == (10, "m.jpg", "image")
    assert out["counters"]["docs_fetched"] == 2


def test_justificacao_sem_sinais_e_baixa(webapi: Any, fake_router: Any) -> None:
    """Mídia sem sinal: base 50, BAIXA, justificativa literal da fonte."""
    _add_media_doc(webapi, 12, {})

    item = _only_item(tool.triage_media(SOURCE_ID))

    assert item["score"] == 50
    assert item["signals"] == []
    assert item["justification"] == "Mídia sem sinais"
    assert item["relevance"] == knowledge.RELEVANCE_BAIXA


# --- (b) os 12 preditores isolados (:28-46) -------------------------------------


@pytest.mark.parametrize(
    ("signal", "props", "points"),
    [
        ("OCR", {"ocrCharCount": ["12"]}, 15),
        ("rosto", {"face_count": ["3"]}, 10),
        ("nudez", {"classeNudez": ["2"]}, 20),
        ("transcrito", {"audio:transcription": ["texto do áudio"]}, 15),
        ("CPF", {"Regex:BR_CPF": ["123.456.789-00"]}, 10),
        ("telefone", {"Regex:PHONE": ["+55 31 9..."]}, 8),
        ("URL", {"Regex:URL": ["https://x.example"]}, 5),
        ("GPS", {"ufed:latitude": ["-22.9"]}, 15),
        ("ext divergente", {"extMismatch": ["true"]}, 10),
        ("carved", {"carved": ["true"]}, 5),
        ("deletado", {"deleted": ["true"]}, 15),
        ("download financeiro", {"ufed:URL": ["http://boleto.example/x"]}, 15),
    ],
)
def test_cada_preditor_isolado(
    webapi: Any, fake_router: Any, signal: str, props: dict, points: int
) -> None:
    """Cada preditor sozinho soma exatamente seus pontos na base 50."""
    _add_media_doc(webapi, 20, props)

    item = _only_item(tool.triage_media(SOURCE_ID))

    expected = (
        ["URL", "download financeiro"]
        if signal == "download financeiro"
        else [signal]
    )
    assert item["signals"] == expected
    # ufed:URL liga também o sinal URL (:35) — único par inseparável da fonte.
    extra = 5 if signal == "download financeiro" else 0
    assert item["score"] == 50 + points + extra


def test_ocr_zero_e_cpf_vazio_nao_sinalizam(webapi: Any, fake_router: Any) -> None:
    """ocrCharCount "0" e Regex:BR_CPF vazio não contam (:29/:33 falsos)."""
    _add_media_doc(webapi, 21, {"ocrCharCount": ["0"], "Regex:BR_CPF": [""]})

    item = _only_item(tool.triage_media(SOURCE_ID))

    assert item["signals"] == []


# --- (c) base chat vs mídia (:70-77) ---------------------------------------------


def test_chat_por_categoria_e_nome_recebe_base_40(
    webapi: Any, fake_router: Any
) -> None:
    """whatsapp na categoria + "chat" no nome: base 40 e rótulo do chat."""
    _add_media_doc(
        webapi, 30, {"ocrCharCount": ["12"]}, category="whatsapp",
        name="chat_entrada.txt",
    )

    out = tool.triage_media(SOURCE_ID)
    item = _only_item(out)

    assert item["score"] == 40 + 15
    assert item["justification"] == "Chat (índice de referências)"
    assert (out["itens_chats"], out["itens_midias"]) == (1, 0)


def test_chat_por_contenttype_entra_sem_categoria_de_midia(
    webapi: Any, fake_router: Any
) -> None:
    """contentType x-ufed-chat marca chat mesmo sem nome "chat" (base 40).

    A coleta MCP só consulta categorias do eixo — o gate por contentType é o
    que decide chat vs mídia DENTRO da categoria consultada (no exemplo,
    o doc de "whatsapp" sem nome de chat vira chat pelo contentType :70-72).
    """
    _add_media_doc(
        webapi, 31, {"contentType": ["application/x-ufed-chat"]},
        category="whatsapp", name="conversa.txt",
    )

    out = tool.triage_media(SOURCE_ID)

    assert out["itens_chats"] == 1
    assert out["ranked_items"][0]["score"] == 40


def test_texto_puro_fora_do_gate(webapi: Any, fake_router: Any) -> None:
    """Categoria sem hint de mídia e sem marca de chat é recusada (:73-74)."""
    _add_media_doc(webapi, 32, {}, category="textos", name="contrato.txt")

    out = tool.triage_media(SOURCE_ID)

    assert out["items_total"] == 0
    assert out["message"] == knowledge.MEDIA_EMPTY_MESSAGE


# --- (d) quirks fiéis da fonte ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "fires"),
    [(["2"], True), (["1"], False), (["1,5"], False), (["3"], True)],
)
def test_classe_nudez_string_br(
    webapi: Any, fake_router: Any, value: list[str], fires: bool
) -> None:
    """classeNudez como string BR passa pelo _num portado (:48-56)."""
    _add_media_doc(webapi, 40, {"classeNudez": value})

    item = _only_item(tool.triage_media(SOURCE_ID))

    assert ("nudez" in item["signals"]) is fires


@pytest.mark.parametrize(
    ("value", "fires"),
    [(["true"], True), (["TRUE"], True), (["sim"], False), (["1"], False)],
)
def test_flag_true_so_casa_true(
    webapi: Any, fake_router: Any, value: list[str], fires: bool
) -> None:
    """Pino D-11: o _true real (:58-63) só casa "true" — NÃO o set do design §6.2."""
    _add_media_doc(webapi, 41, {"extMismatch": value})

    item = _only_item(tool.triage_media(SOURCE_ID))

    assert ("ext divergente" in item["signals"]) is fires


def test_download_financeiro_quirk_registrato(
    webapi: Any, fake_router: Any
) -> None:
    """Literal fiel :43: "registrato" casa; "registrado" (PT correto) NÃO."""
    _add_media_doc(webapi, 42, {"ufed:URL": ["https://registrato.example/x"]})
    _add_media_doc(webapi, 43, {"ufed:URL": ["https://registrado.example/x"]})

    out = tool.triage_media(SOURCE_ID)

    by_id = {i["doc_id"]: i for i in out["ranked_items"]}
    assert "download financeiro" in by_id[42]["signals"]
    assert "download financeiro" not in by_id[43]["signals"]


# --- (e) relevância inclusiva do handler (:965) ------------------------------------


@pytest.mark.parametrize(
    ("props", "score", "relevance"),
    [
        ({"classeNudez": ["2"], "audio:transcription": ["t"]}, 85,
         knowledge.RELEVANCE_ALTA),
        ({"face_count": ["1"]}, 60, knowledge.RELEVANCE_MEDIA),
        ({"Regex:PHONE": ["+55"]}, 58, knowledge.RELEVANCE_BAIXA),
    ],
)
def test_relevancia_inclusiva_85_60(
    webapi: Any, fake_router: Any, props: dict, score: int, relevance: str
) -> None:
    """Handler :965 é INCLUSIVO: 85→ALTA e 60→MÉDIA (diferente de geo/telephony)."""
    _add_media_doc(webapi, 50, props)

    item = _only_item(tool.triage_media(SOURCE_ID))

    assert (item["score"], item["relevance"]) == (score, relevance)


# --- (f) agregados _media_owner_summary (draft_report:1500-1537) --------------------


def test_histograma_de_categoria_crua_e_ni(webapi: Any, fake_router: Any) -> None:
    """by_category com categoria crua e chat sem categoria → "N/I" (:1509)."""
    _add_media_doc(webapi, 60, {}, category="image")
    _add_media_doc(webapi, 61, {}, category="image")
    _add_media_doc(
        webapi, 62, {"contentType": ["application/x-ufed-chat"]},
        category="image", props_category="", name="c.txt",
    )

    out = tool.triage_media(SOURCE_ID)

    assert out["by_category"] == {"image": 2, "N/I": 1}


def test_apps_por_path_e_service_identifier(webapi: Any, fake_router: Any) -> None:
    """Marcador com.whatsapp no path e ufed:ServiceIdentifier somam (:1519-1524)."""
    _add_media_doc(webapi, 63, {}, path="/data/com.whatsapp/databases/msg.db")
    _add_media_doc(webapi, 64, {"ufed:ServiceIdentifier": ["com.google.services"]})

    out = tool.triage_media(SOURCE_ID)

    assert out["apps"] == {"WhatsApp": 1, "com.google.services": 1}


def test_owner_signals_dedup_ni_ignorado(webapi: Any, fake_router: Any) -> None:
    """Titulares: dedup preserva ordem; "N/I" ignorado (:1525-1528)."""
    _add_media_doc(
        webapi, 65, {"ufed:Account": ["FULANO"], "ufed:PhoneOwner": ["N/I"]}
    )
    _add_media_doc(
        webapi, 66, {"ufed:Account": ["FULANO"], "common:dc:creator": ["CICLANO"]}
    )

    out = tool.triage_media(SOURCE_ID)

    assert out["owner_media_signals"] == ["FULANO", "CICLANO"]


def test_owner_signals_teto_20_marcado(webapi: Any, fake_router: Any) -> None:
    """22 titulares únicos → 20 publicados (:1533 signals[:20])."""
    for i in range(22):
        _add_media_doc(webapi, 70 + i, {"Conversation:Account": [f"titular-{i:02d}"]})

    out = tool.triage_media(SOURCE_ID)

    assert len(out["owner_media_signals"]) == knowledge.MEDIA_OWNER_SIGNALS_MAX


# --- (g) histograma de campos com poda MEDIA_DROP (§6.2) ----------------------------


def test_field_hits_dropa_chaves_de_blob(webapi: Any, fake_router: Any) -> None:
    """Chaves com substring de MEDIA_DROP saem do histograma (:75-76/:800)."""
    _add_media_doc(
        webapi, 80,
        {
            "ocrCharCount": ["12"],
            "face_encodings": ["AAAA"],
            "thumb": ["QUJD"],
            "ufed:base64data": ["x"],
        },
    )

    out = tool.triage_media(SOURCE_ID)
    hits = out["counters"]["field_hits"]

    assert "ocrCharCount" in hits
    for key in BLOB_KEYS:
        assert key not in hits
    for key in hits:
        assert not any(d in key.lower() for d in knowledge.MEDIA_DROP_KEYS)


def test_field_hits_somente_nomes_sem_valores(webapi: Any, fake_router: Any) -> None:
    """Histograma é nome→contagem (jamais valor); valores só no agregado fiel.

    O valor de ``ufed:Account`` aparece em ``owner_media_signals`` porque o
    ``_media_owner_summary`` do ICARO publica titulares de propósito
    (draft_report:1525-1533) — o pino de privacidade (T-10) vale para o
    histograma de campos, que nunca carrega valor.
    """
    _add_media_doc(webapi, 81, {"ufed:Account": ["CPF DO TITULAR 000.000.000-00"]})

    out = tool.triage_media(SOURCE_ID)
    hits = out["counters"]["field_hits"]

    assert hits["ufed:Account"] == 1
    assert all(isinstance(count, int) for count in hits.values())
    assert "CPF DO TITULAR 000.000.000-00" in out["owner_media_signals"]


# --- (h) teto de itens com total pré-teto -------------------------------------------


def test_teto_50_itens_marcado_com_omissao(webapi: Any, fake_router: Any) -> None:
    """52 itens: entrega 50, items_total 52, truncated com 2 e ordem por score."""
    _add_media_doc(webapi, 200, {"ocrCharCount": ["12"]})  # 65, vence a lista
    for i in range(51):
        _add_media_doc(webapi, 201 + i, {}, name=f"f{i}.jpg")

    out = tool.triage_media(SOURCE_ID)

    assert len(out["ranked_items"]) == knowledge.MEDIA_ITEMS_MAX
    assert out["items_total"] == 52
    assert out["truncated"]["items_truncated"] is True
    assert out["truncated"]["items_omitted"] == 2
    assert out["ranked_items"][0]["score"] == 65


# --- (i) warning de cobertura GPS ----------------------------------------------------


def test_gps_zero_gera_warning_de_cobertura(webapi: Any, fake_router: Any) -> None:
    """Mídia varrida sem nenhum GPS: warning nomeando as variantes tentadas."""
    _add_media_doc(webapi, 90, {"ocrCharCount": ["12"]})

    out = tool.triage_media(SOURCE_ID)

    warning = [w for w in out["warnings"] if "cobertura GPS = 0" in w]
    assert len(warning) == 1
    assert "latitude/ufed:latitude" in warning[0]


def test_gps_presente_nao_gera_warning(webapi: Any, fake_router: Any) -> None:
    """Um único GPS já desliga o warning de cobertura."""
    _add_media_doc(webapi, 91, {"latitude": ["-22.9"]})

    out = tool.triage_media(SOURCE_ID)

    assert not [w for w in out["warnings"] if "cobertura GPS" in w]


# --- (j) contratos M-2, orçamento e exatidão de GETs ----------------------------------


def test_query_500_vira_hit_negativo_e_warning_sem_parar(
    webapi: Any, fake_router: Any
) -> None:
    """500 numa variante: -1 + warning nominal; variante e grupos seguem."""
    webapi.fail_search_queries.add('category:"video"')
    _add_media_doc(webapi, 100, {}, category="image")

    out = tool.triage_media(SOURCE_ID)

    assert out["query_hits"]['category:"video"'] == -1
    assert out["query_hits"]['category:"videos"'] == 0  # sondou a variante
    assert any('category:"video"' in w and "falhou" in w for w in out["warnings"])
    assert out["query_hits"][IMAGE_QUERY] == 1
    assert out["items_total"] == 1


def test_source_inexistente_vira_value_error_estavel(fake_router: Any) -> None:
    """404 de source: ValueError estável ANTES de qualquer HTTP (M-2)."""
    with pytest.raises(ValueError, match="não encontrado em nenhum endpoint"):
        tool.triage_media(UNKNOWN_SOURCE)


def test_eixo_vazio_e_mensagem_explicita_sem_excecao(
    webapi: Any, fake_router: Any
) -> None:
    """Nada de mídia: message explícita + as 19 probes dos grupos pagas.

    M-TR-1: cada categoria vira um grupo ordenado de variantes; com 0 hit
    em tudo, paga-se o grupo inteiro (21 literais em knowledge.
    MEDIA_CATEGORY_VARIANTS) e cada grupo gera warning nominal (M-7).
    """
    out = tool.triage_media(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS | {"message"}
    assert out["message"] == knowledge.MEDIA_EMPTY_MESSAGE
    probes = sum(len(g) for g in knowledge.MEDIA_CATEGORY_VARIANTS)
    assert probes == 21
    assert out["counters"]["http_calls"] == probes
    zero = [w for w in out["warnings"] if "variantes sondadas sem hits" in w]
    assert len(zero) == len(knowledge.MEDIA_CATEGORY_VARIANTS) == 10


def test_orcamento_estourado_vira_parcial_marcado(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap 13 tingido no laço de /search: 1 doc entra, 3 fora — nunca exc.

    M-TR-1 (ordem real pinnada): image vence na 1ª variante (1 search, 4
    candidatos); video (2) + audio (2) + document (3) + pdf (2) + download
    (2) + spreadsheet (2) zerados = 12; presentation paga só a 1ª variante
    (13 = cap) e morre antes de "presentations". Os 4 candidatos ficam sem
    /docs: ``docs_omitted_by_budget == 4`` e o eixo fecha VAZIO mas
    MARCADO (partial + truncated + warning + message) — o corte no tráfego
    de /search nunca é exceção nem silêncio (política R-TRIAGE-1).
    """
    monkeypatch.setitem(knowledge.TOOL_BUDGETS, "triage_media", 13)
    for i in range(4):
        _add_media_doc(webapi, 300 + i, {}, name=f"b{i}.jpg")

    out = tool.triage_media(SOURCE_ID)

    counters, truncated = out["counters"], out["truncated"]
    assert (counters["phase"], counters["http_calls"]) == (
        progress.PHASE_PARTIAL, 13,
    )
    assert truncated["http_budget_exceeded"] is True
    assert truncated["http_budget_calls_at_stop"] == 13
    assert truncated["docs_omitted_by_budget"] == 4
    assert "category:\"presentations\"" not in out["query_hits"]  # cortada
    assert any("http_budget_exceeded" in w and "cap 13" in w for w in out["warnings"])
    assert out["items_total"] == 0 and "message" in out  # vazio, mas partial
    assert out["counters"]["phase"] == progress.PHASE_PARTIAL



def test_http_calls_exato_e_igual_ao_trafego_do_fake(
    webapi: Any, fake_router: Any
) -> None:
    """Pino de exatidão: 11 probes (image vence cedo) + N docs == fake."""
    for i in range(3):
        _add_media_doc(webapi, 310 + i, {}, name=f"c{i}.jpg")
    webapi.requests_log.clear()

    out = tool.triage_media(SOURCE_ID)

    paths = [entry["path"] for entry in webapi.requests_log]
    searches = paths.count("/search")
    docs = sum("/docs/" in p for p in paths)
    assert searches == 19  # image vence na 1ª variante; os demais zeram o grupo
    assert out["counters"]["http_calls"] == searches + docs == 19 + 3
    assert out["truncated"]["http_budget_exceeded"] is False


# --- registro MCP ---------------------------------------------------------------------


def test_tool_registrada_via_mcp(fake_router: Any, webapi: Any) -> None:
    """triage_media é descoberta e chamada pelo MCP (mesmo mecanismo)."""
    from fastmcp import Client, FastMCP

    from iped_mcp.triage import register as register_triage

    server = FastMCP("iped_mcp_test")
    register_triage(server)

    async def _run() -> dict[str, Any]:
        async with Client(server) as client:
            names = [t.name for t in await client.list_tools()]
            assert "triage_media" in names
            result = await client.call_tool("triage_media", {"source_id": SOURCE_ID})
            return (
                result.data
                if result.data is not None
                else json.loads(result.content[0].text)
            )

    payload = asyncio.run(_run())
    assert payload["source_id"] == SOURCE_ID
    assert payload["message"] == knowledge.MEDIA_EMPTY_MESSAGE


# --- M-TR-1: variantes de categoria (singular legado + plural do jar) -------------


def test_mtr1_plural_visto_quando_singular_da_zero(
    webapi: Any, fake_router: Any
) -> None:
    """F1 reproduzida no fake (matcher exato): só "images" casa → categoria vista.

    No jar real image=0 vs images=38.057; o fake tem o MESMO matcher
    phrase-exato, então este pino exercita o caminho exato da F1: a literal
    do ICARO é tentada primeiro (0), a variante plural entra e o item é
    triado normalmente.
    """
    _add_media_doc(webapi, 900, {"ocrCharCount": ["10"]}, category="images",
                   name="evid.jpg")

    out = tool.triage_media(SOURCE_ID)

    assert out["query_hits"]['category:"image"'] == 0
    assert out["query_hits"]['category:"images"'] == 1
    assert out["items_total"] == 1
    assert out["ranked_items"][0]["category"] == "images"
    assert not [
        w for w in out["warnings"]
        if "categoria 'image'" in w and "variantes sondadas" in w
    ]  # grupo com hit NÃO gera warning de 0


def test_mtr1_singular_legado_vence_e_parou_na_primeira_variante(
    webapi: Any, fake_router: Any
) -> None:
    """Literal do ICARO vence quando existe: "videos" NUNCA é sondado.

    Paridade com o ICARO preservada (o que ele veria, o MCP vê) e custo
    mínimo (primeiro hit vence — sem GETs extras pela categoria).
    """
    _add_media_doc(webapi, 905, {}, category="video", name="v.mp4")
    webapi.requests_log.clear()

    out = tool.triage_media(SOURCE_ID)

    assert out["query_hits"]['category:"video"'] == 1
    assert 'category:"videos"' not in out["query_hits"]
    assert 'category:"vídeos"' not in out["query_hits"]
    assert out["items_total"] == 1


def test_mtr1_grupo_inteiro_zero_gera_warning_nominal(
    webapi: Any, fake_router: Any
) -> None:
    """0 em todas as variantes do grupo: warning nomeia a categoria legada.

    O "image=0" silencioso da F1 não se repete: o envelope denuncia o
    grupo inteiro (3 variantes) e aponta o censo (triage_query_discovery)
    como fonte de verdade — filosofia M-7.
    """
    _add_media_doc(webapi, 910, {}, category="images", name="x.jpg")

    out = tool.triage_media(SOURCE_ID)

    zero = [w for w in out["warnings"] if "variantes sondadas sem hits" in w]
    texts = " | ".join(zero)
    assert "categoria 'video'" in texts  # grupo 2 zerado (3 variantes)
    assert "3 variantes" in texts
    assert "categoria 'image'" not in texts  # image teve hit via variante


def test_mtr1_teto_e_histograma_intactos_com_variantes(
    webapi: Any, fake_router: Any
) -> None:
    """Docs vindos pela variante passam pelo MESMO gate/teto/histograma T-6."""
    for i in range(3):
        _add_media_doc(webapi, 920 + i, {"face_count": ["2"]},
                       category="imagens", name=f"p{i}.jpg")

    out = tool.triage_media(SOURCE_ID)

    assert out["items_total"] == 3
    assert all("rosto" in item["signals"] for item in out["ranked_items"])
    assert out["truncated"]["total_cap_applied"] is False
    assert out["counters"]["field_hits"]  # histograma de nomes populated
