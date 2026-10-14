"""T-10 (DESIGN 09-B §10.5): triage_query_discovery — catálogo por evidência.

Casos pinados: descoberta com campos (histograma só com NOMES + cobertura e
probes_que_o_veem[:3]) (a), sonda de detectores restrita a KNOWN_TYPES com
pino estrutural anti-deriva e warning nominal quando nenhum detector
responde (b), cache por source: 2ª chamada com http_calls==0 +
catalogo_servido_de_cache e TTL expirado re-sondando (c), probe com 500 →
available false + erro sem abortar — inclusive contra o fake real (d),
tetos de amostra (5 docs/probe) e de probes_que_o_veem (3) (e), pin de
privacidade: nenhum VALOR de conteúdo no envelope (f), contratos M-2
(source 404, sid vazio) e catálogo vazio com mensagem (g), orçamento
R-TRIAGE-1 com estouro truncado NUNCA cacheado e http_calls exato (h).
Backend: FakeWebAPI da suíte + search_ids roteado por script; nenhum HTTP
real.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from iped_mcp import progress
from iped_mcp.regex.queries import KNOWN_TYPES
from iped_mcp.triage import discovery, knowledge, tool
from tests.conftest import SOURCE_ID, UNKNOWN_SOURCE, make_doc

SECRET_VALUE = "FRASE-SECRETA-QUE-NAO-PODE-SAIR"
ENVELOPE_KEYS = {
    "source_id",
    "probes",
    "fields",
    "regex_availability",
    "mime_types",
    "extensions",
    "sample_size",
    "cache",
    "counters",
    "truncated",
    "warnings",
}


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zera o cache de catálogo por teste (estado de módulo, não conftest)."""
    monkeypatch.setattr(discovery, "_CACHE", {})


def _doc(api: Any, doc_id: int, props: dict[str, Any]) -> None:
    """Registra doc no fake (props respondidas pelo /docs real do fake)."""
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, f"d{doc_id}", props)


def _script_search(
    monkeypatch: pytest.MonkeyPatch,
    api: Any,
    hits_by_term: dict[str, list[int]],
    calls: list[str],
    fail_terms: tuple[str, ...] = (),
    regex_docs: bool = True,
) -> None:
    """Roteia ``discovery.search_ids`` por script (frase → hits; Regex → props).

    Args:
        monkeypatch: pytest monkeypatch.
        api: estado do FakeWebAPI (docs para a sonda Regex fiel ao fake).
        hits_by_term: hits por probe de frase literal; demais = 0 hits.
        calls: lista observável de queries emitidas (pino de cache/orçamento).
        fail_terms: frases que respondem 500 (ValueError como o fetch real).
        regex_docs: False → sondas Regex sempre vazias (source sem
            detectores; pino do warning nominal).
    """

    def fake_search(
        query: str, source_id: str, hint: str, timeout: float | None = None
    ) -> list[int]:
        """Script do roteador: registra a query e devolve hits/falha."""
        calls.append(query)
        unescaped = query.replace("\\:", ":")
        if unescaped in fail_terms:
            raise ValueError(f"query rejeitada (500): {unescaped}")
        if unescaped.startswith("Regex:"):
            if not regex_docs:
                return []
            key = f"Regex:{unescaped.split(':', 2)[1]}"
            return [
                i
                for (src, i), d in api.docs.items()
                if src == source_id and key in d.get("properties", {})
            ]
        return list(hits_by_term.get(unescaped, []))

    monkeypatch.setattr(discovery, "search_ids", fake_search)


def _rich_doc(api: Any, doc_id: int) -> None:
    """Doc de amostra com mime/extensão, lista, escalar e valor secreto."""
    _doc(
        api,
        doc_id,
        {
            "name": [f"arquivo_{doc_id}.png"],
            "Mime type": ["image/png"],
            "Extension": ["png"],
            "texto_extraido": [SECRET_VALUE],
            "altura": [str(100 + doc_id)],
        },
    )


def _assert_histograma_amostra(out: dict[str, Any]) -> None:
    """Shape de amostra do catálogo: campos com cobertura, mime/extensão.

    Args:
        out: envelope de ``triage_query_discovery`` (build fresco pinnado).

    Returns:
        Nada.
    """
    field = out["fields"]["name"]
    assert field["docs_na_amostra"] == 2
    assert field["probes_que_o_veem"] == ["whatsapp"]
    assert field["value_type"] == "list"
    assert out["fields"]["altura"]["value_type"] == "list"
    assert out["mime_types"] == {"image/png": 2}
    assert out["extensions"] == {"png": 2}
    assert out["sample_size"] == knowledge.DISCOVERY_SAMPLE_SIZE
    assert knowledge.DISCOVERY_REGEX_SONDA_WARNING in out["warnings"]


def _assert_served_from_cache(second: dict[str, Any]) -> None:
    """Contrato de cache-visível (R-TRIAGE-1): 0 GETs, flags e warning.

    Args:
        second: envelope da 2ª chamada dentro do TTL.

    Returns:
        Nada.
    """
    assert second["cache"]["served_from_cache"] is True
    assert second["counters"]["http_calls"] == 0  # PIN: cache não conta GET
    assert second["counters"]["cached"] is True
    assert second["cache"]["ttl_s"] == knowledge.DISCOVERY_CACHE_TTL
    assert second["cache"]["age_s"] >= 0
    assert any(
        w.startswith("catalogo_servido_de_cache") for w in second["warnings"]
    )


# --- (a) descoberta com campos -------------------------------------------------


def test_descoberta_com_campos_histograma_e_contagem_exata(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe com hits amostra 2 docs: histograma de nomes, mime e http_calls."""
    _rich_doc(webapi, 10)
    _rich_doc(webapi, 11)
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {"whatsapp": [10, 11]}, calls)

    out = tool.triage_query_discovery(SOURCE_ID)

    assert set(out) == ENVELOPE_KEYS
    assert out["probes"]["whatsapp"] == {"total_hits": 2, "available": True}
    assert out["probes"]["telegram"] == {"total_hits": 0, "available": True}
    _assert_histograma_amostra(out)
    # 33 probes + 2 props (docs distintos; dedupe de props entre probes) + 9
    assert out["counters"]["http_calls"] == 33 + 2 + 9
    assert out["counters"]["probes_ran"] == 33
    assert out["cache"]["served_from_cache"] is False


def test_value_type_scalar_e_mixed(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Campo com valor escalar → str; doc da mesma lista com outro tipo → mixed."""
    _doc(webapi, 10, {"campo_x": ["v"]})
    _doc(webapi, 11, {"campo_x": "escalar"})
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {"gps": [10, 11]}, calls)

    out = tool.triage_query_discovery(SOURCE_ID)

    assert out["fields"]["campo_x"]["value_type"] == "mixed"
    assert out["fields"]["campo_x"]["docs_na_amostra"] == 2


# --- (b) sonda de detectores ----------------------------------------------------


def test_sonda_e_known_types_pino_estrutural(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sonda cobre exatamente os 9 canônicos — pino contra deriva da lista."""
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {}, calls)

    out = tool.triage_query_discovery(SOURCE_ID)

    assert set(out["regex_availability"]) == set(KNOWN_TYPES)
    assert len(KNOWN_TYPES) == 9
    # O doc padrão do fake carrega Regex:EMAIL → sonda fiel às propriedades.
    assert out["regex_availability"]["EMAIL"] >= 1
    regex_queries = [q for q in calls if q.replace("\\:", ":").startswith("Regex:")]
    assert len(regex_queries) == 9


def test_source_sem_detectores_regex_avisa(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nenhum detector responde hits → warning nominal (nunca 0-silencioso)."""
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {}, calls, regex_docs=False)

    out = tool.triage_query_discovery(SOURCE_ID)

    assert set(out["regex_availability"].values()) == {0}
    assert knowledge.DISCOVERY_NO_REGEX_DETECTOR_WARNING in out["warnings"]


# --- (c) cache por source --------------------------------------------------------


def test_cache_segunda_chamada_zero_gets(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2ª chamada na janela: 0 GETs novos, served_from_cache e warning visível."""
    _rich_doc(webapi, 10)
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {"whatsapp": [10]}, calls)

    first = tool.triage_query_discovery(SOURCE_ID)
    calls_after_first = list(calls)
    second = tool.triage_query_discovery(SOURCE_ID)

    assert first["cache"]["served_from_cache"] is False
    assert calls == calls_after_first  # PIN: nenhuma query nova
    assert second["fields"] == first["fields"]
    assert second["probes"] == first["probes"]
    _assert_served_from_cache(second)


def test_cache_ttl_expirado_re_sonda(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entrada mais velha que o TTL → build fresco (GETs > 0, served false)."""
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {}, calls)
    tool.triage_query_discovery(SOURCE_ID)

    entry = discovery._CACHE[SOURCE_ID]
    discovery._CACHE[SOURCE_ID] = discovery._CacheEntry(
        built_at=entry.built_at - knowledge.DISCOVERY_CACHE_TTL - 1.0,
        payload=entry.payload,
    )
    out = tool.triage_query_discovery(SOURCE_ID)

    assert out["cache"]["served_from_cache"] is False
    assert out["counters"]["http_calls"] > 0


def test_cache_nao_vaza_entre_sources(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache é POR SOURCE: outro source não serve o catálogo alheio."""
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {}, calls)
    tool.triage_query_discovery(SOURCE_ID)

    entry = discovery._CACHE[SOURCE_ID]
    # Outro source legítimo (o fake só conhece SOURCE_ID) exige resolve real;
    # pino estrutural: a chave do cache é o source consultado.
    assert list(discovery._CACHE) == [SOURCE_ID]
    assert entry.payload["sample_size"] == knowledge.DISCOVERY_SAMPLE_SIZE


# --- (d) falha de probe ------------------------------------------------------------


def test_probe_500_nao_aborta(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe que falha: available false + erro + warning; o restante executa."""
    calls: list[str] = []
    _script_search(
        monkeypatch, webapi, {"msgstore": [10]}, calls, fail_terms=("whatsapp",)
    )

    out = tool.triage_query_discovery(SOURCE_ID)

    failed = out["probes"]["whatsapp"]
    assert failed["available"] is False
    assert failed["total_hits"] is None
    assert "500" in failed["error"]
    assert out["probes"]["msgstore"]["available"] is True
    assert len(out["probes"]) == 33  # todos os probes presentes
    assert any(w.startswith("probe_falhou: 'whatsapp'") for w in out["warnings"])


def test_probe_500_contra_fake_real(
    webapi: Any, fake_router: Any
) -> None:
    """500 real do fake (fail_search_queries): mesmo contrato sem script."""
    webapi.fail_search_queries.add("whatsapp")

    out = tool.triage_query_discovery(SOURCE_ID)

    assert out["probes"]["whatsapp"]["available"] is False
    assert out["probes"]["whatsapp"]["error"]
    assert out["probes"]["pix"]["available"] is True


# --- (e) tetos ----------------------------------------------------------------------


def test_teto_de_amostra_5_docs_por_probe(
    webapi: Any,
    fake_router: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe com 7 hits: props buscadas só para os 5 primeiros (M-7)."""
    for doc_id in range(20, 27):
        _rich_doc(webapi, doc_id)
    calls: list[str] = []
    _script_search(
        monkeypatch, webapi, {"whatsapp": list(range(20, 27))}, calls
    )
    props_calls: list[int] = []
    real_props = discovery.get_document_props

    def counting(sid: str, doc_id: int) -> dict[str, Any] | None:
        """Registra o GET de props e delega ao fetch real."""
        props_calls.append(doc_id)
        return real_props(sid, doc_id)

    monkeypatch.setattr(discovery, "get_document_props", counting)

    out = tool.triage_query_discovery(SOURCE_ID)

    assert props_calls == list(range(20, 25))
    assert out["fields"]["name"]["docs_na_amostra"] == 5
    assert out["counters"]["docs_sampled"] == 5


def test_teto_probes_que_o_veem_trs(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4 probes veem o mesmo campo: lista sai truncada em 3 (shape §10.3)."""
    _rich_doc(webapi, 10)
    hits = {term: [10] for term in ("whatsapp", "sms", "email", "pix")}
    calls: list[str] = []
    _script_search(monkeypatch, webapi, hits, calls)

    out = tool.triage_query_discovery(SOURCE_ID)

    seen = out["fields"]["name"]["probes_que_o_veem"]
    assert len(seen) == knowledge.DISCOVERY_FIELD_PROBES_MAX
    assert set(seen).issubset(set(hits))
    assert out["fields"]["name"]["docs_na_amostra"] == 1  # doc único, sem dupla contagem


# --- (f) pin de privacidade -----------------------------------------------------------


def test_pin_privacidade_valores_fora_do_envelope(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nenhum VALOR de conteúdo cruza o envelope — só nomes e contagens."""
    _rich_doc(webapi, 10)
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {"whatsapp": [10]}, calls)

    out = tool.triage_query_discovery(SOURCE_ID)

    assert SECRET_VALUE not in json.dumps(out, ensure_ascii=False)
    assert "arquivo_10.png" not in json.dumps(out, ensure_ascii=False)
    for entry in out["fields"].values():
        assert set(entry) == {
            "docs_na_amostra",
            "probes_que_o_veem",
            "value_type",
        }


# --- (g) contratos M-2 e vazio -----------------------------------------------------------


def test_source_inexistente_404_sem_http(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source inexistente: ValueError M-2 ANTES de qualquer query."""
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {}, calls)

    with pytest.raises(ValueError, match=UNKNOWN_SOURCE):
        tool.triage_query_discovery(UNKNOWN_SOURCE)

    assert calls == []  # PIN: M-2 antes de HTTP


def test_sid_vazio_rejeitado() -> None:
    """source_id vazio/None → ValueError estável (família triage)."""
    with pytest.raises(ValueError, match="obrigat"):
        tool.triage_query_discovery("  ")


def test_catalogo_vazio_com_mensagem(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nada em probes/campos/detectores → message explícita, nunca exceção."""
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {}, calls, regex_docs=False)

    out = tool.triage_query_discovery(SOURCE_ID)

    assert out["message"] == knowledge.DISCOVERY_EMPTY_MESSAGE
    assert out["fields"] == {}
    assert all(p["total_hits"] == 0 for p in out["probes"].values())


# --- (h) orçamento R-TRIAGE-1 --------------------------------------------------------------


def test_orcamento_estourado_truncado_e_nao_cacheado(
    webapi: Any,
    fake_router: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap 2: para no 2º GET, marca truncated+partial, avisa e NÃO cacheia."""
    monkeypatch.setitem(
        knowledge.TOOL_BUDGETS, "triage_query_discovery", 2
    )
    calls: list[str] = []
    _script_search(monkeypatch, webapi, {"whatsapp": [10]}, calls)

    out = tool.triage_query_discovery(SOURCE_ID)

    assert out["truncated"]["http_budget_exceeded"] is True
    assert out["counters"]["http_calls"] == 2
    assert out["counters"]["phase"] == progress.PHASE_PARTIAL
    assert any(w.startswith("http_budget_exceeded") for w in out["warnings"])
    assert len(out["probes"]) == 2  # demais probes nem executados
    assert SOURCE_ID not in discovery._CACHE  # parcial nunca serve de cache
