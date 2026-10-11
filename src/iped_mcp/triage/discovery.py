"""Tool ``triage_query_discovery`` — descoberta de superfície do source (T-10).

Responde "O QUE EXISTE neste caso?" por fonte — substituto determinístico
da descoberta manual ``iped-icaro/iped_query_discovery.py``. CATÁLOGO POR
EVIDÊNCIA (DESIGN 09-B §10): probes de frase LUCENE (a lista literal do
ICARO :39-106, recontada em 33 termos — design §12), amostra de até
``DISCOVERY_SAMPLE_SIZE`` docs por probe com histograma de CAMPOS
(:319-323/:389-476) e sonda de detectores ``Regex:<N>`` restrita aos 9 tipos
canônicos do servidor (``regex/queries.py:11-21``; o RegexConfig.txt varrido
pelo ICARO :204-282 é externo ao MCP — o warning ``sonda=KNOWN_TYPES``
documenta a diferença). Cache por source com TTL 300 s (mesma política de
``fetch.py:25``, precedente M-6/M-7): a 2ª chamada na janela custa 0 GETs e
avisa ``catalogo_servido_de_cache`` (R-TRIAGE-1).

Pin de privacidade (§10.2, decisão RECOMMENDATION): o envelope traz
SOMENTE NOMES de campos, cobertura e ``value_type`` — nunca valores de
conteúdo (os "examples" do catálogo LOCAL do ICARO não atravessam para o
envelope, que é lido por um LLM). Falha de probe (500/timeout) vira
``available: false`` + erro nominal e NUNCA aborta o restante (:338-353).
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP

from iped_mcp import progress
from iped_mcp.fetch import get_document_props, search_ids
from iped_mcp.progress import op_envelope
from iped_mcp.regex.queries import KNOWN_TYPES, build_regex_query
from iped_mcp.router import get_router
from iped_mcp.triage import knowledge
from iped_mcp.triage.budget import HttpBudget as _Budget

_SEARCH_HINT = "triage_query_discovery (probe de frase/sonda Regex)"
_EMPTY_SID_MESSAGE = "'source_id' é obrigatório (veja iped_list_sources)."
_BUDGET_OMITTED = "probes e amostras restantes não executados"
_TYPE_LIST = "list"
_TYPE_STR = "str"
_TYPE_MIXED = "mixed"
_REGEX_FAILED_HITS = -1  # convenção query_hits: -1 = query que falhou


@dataclass
class _CacheEntry:
    """Catálogo construído e o instante de construção (cache por source).

    Attributes:
        built_at: ``time.monotonic()`` do build (base do TTL).
        payload: catálogo serializável (com ``_warnings``/``_docs_sampled``
            privados, fundidos no envelope por ``_envelope``).
    """

    built_at: float
    payload: dict[str, Any]


_CACHE: dict[str, _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()


@dataclass
class _Build:
    """Resultado de um build fresco do catálogo (antes do envelope).

    Attributes:
        payload: catálogo cru (shape §10.3 + chaves privadas ``_warnings``
            e ``_docs_sampled``).
        docs_sampled: nº de pares (probe, doc) efetivamente inspecionados.
        exceeded: True quando o orçamento de GET cortou o build (o catálogo
            parcial NUNCA entra no cache).
    """

    payload: dict[str, Any] = field(default_factory=dict)
    docs_sampled: int = 0
    exceeded: bool = False


def _register_value(target: dict[str, int], value: Any) -> None:
    """Conta valores de um props em ``target`` (fiel a ``_register_value``).

    Fiel a ``iped_query_discovery.py:478-499``: None ignora; lista conta cada
    item; o valor vira chave ``str`` com contagem acumulada. Usada APENAS
    para os buckets de tipo (mime/extension) do shape §10.3 — nomes de
    arquivos e valores de conteúdo ficam fora (pin de privacidade).

    Args:
        target: bucket de contagem (``mime_types``/``extensions``).
        value: valor cru da propriedade (str, lista ou None).

    Returns:
        Nada.
    """
    if value is None:
        return
    items = value if isinstance(value, list) else [value]
    for item in items:
        if item is None:
            continue
        key = str(item)
        target[key] = target.get(key, 0) + 1


def _run_probes(
    sid: str, budget: _Budget
) -> tuple[dict[str, Any], dict[str, list[int]], list[str]]:
    """Executa os probes de frase (§10.2.1; 500 não aborta, fiel :338-353).

    Args:
        sid: source roteado.
        budget: orçamento de GETs da chamada.

    Returns:
        ``(probes, hit_ids, warnings)`` — ``probes`` no shape
        ``{termo: {total_hits, available, error?}}``; ``hit_ids`` só com os
        probes que tiveram hits (fonte da amostragem); warnings nomeiam cada
        probe que falhou.
    """
    probes: dict[str, Any] = {}
    hit_ids: dict[str, list[int]] = {}
    warnings: list[str] = []
    for term in knowledge.DISCOVERY_PROBES:
        if not budget.allows():
            budget.exceeded = True
            break
        budget.record()
        try:
            ids = search_ids(term, sid, hint=_SEARCH_HINT)
        except ValueError as exc:
            probes[term] = {
                "total_hits": None,
                "available": False,
                "error": str(exc),
            }
            warnings.append(
                knowledge.DISCOVERY_PROBE_FAILED_WARNING_TEMPLATE.format(
                    probe=term, error=exc
                )
            )
            continue
        probes[term] = {"total_hits": len(ids), "available": True}
        if ids:
            hit_ids[term] = ids
    return probes, hit_ids, warnings


def _inspect_props(
    term: str,
    doc_id: int,
    props: dict[str, Any],
    fields: dict[str, dict[str, Any]],
    mime_types: dict[str, int],
    extensions: dict[str, int],
) -> None:
    """Acumula o histograma de NOMES de campos de um doc amostrado.

    Fiel a ``_inspect_properties`` (:389-476) no que o MCP pode expor: nome
    do campo, docs que o têm e valor ``str``/``list``; os "examples" do
    ICARO ficam de fora (privacidade — docstring do módulo). mime/extension
    alimentam os buckets de tipo do shape §10.3 por substring da chave
    (:439/:450).

    Args:
        term: probe que levou ao doc.
        doc_id: doc inspecionado.
        props: propriedades do doc.
        fields: histograma acumulador (mutado).
        mime_types: bucket de mimes (mutado).
        extensions: bucket de extensões (mutado).

    Returns:
        Nada.
    """
    for key, value in props.items():
        entry = fields.setdefault(
            key, {"docs": set(), "probes": [], "types": set()}
        )
        entry["docs"].add(doc_id)
        if (
            term not in entry["probes"]
            and len(entry["probes"]) < knowledge.DISCOVERY_FIELD_PROBES_MAX
        ):
            entry["probes"].append(term)
        entry["types"].add(_TYPE_LIST if isinstance(value, list) else _TYPE_STR)
        key_lower = key.lower()
        if knowledge.DISCOVERY_MIME_KEY_TOKEN in key_lower:
            _register_value(mime_types, value)
        if knowledge.DISCOVERY_EXTENSION_KEY_TOKEN in key_lower:
            _register_value(extensions, value)


def _collect_samples(
    sid: str, hit_ids: dict[str, list[int]], budget: _Budget
) -> tuple[dict[str, dict[str, Any]], dict[str, int], dict[str, int], int]:
    """Amostra até ``DISCOVERY_SAMPLE_SIZE`` docs por probe com hits (M-7).

    Props de um mesmo doc são reaproveitadas entre probes (mesmo GET —
    contagem honesta em ``http_calls``); doc com props ``None`` (removido)
    não entra no histograma nem no par-inspecionado.

    Args:
        sid: source roteado.
        hit_ids: ids por probe com hits (ordem dos probes preservada).
        budget: orçamento de GETs da chamada.

    Returns:
        ``(fields_raw, mime_types, extensions, docs_sampled)``; o loop para
        marcando ``budget.exceeded`` quando o orçamento acaba.
    """
    fields: dict[str, dict[str, Any]] = {}
    mime_types: dict[str, int] = {}
    extensions: dict[str, int] = {}
    props_cache: dict[int, dict[str, Any] | None] = {}
    docs_sampled = 0
    for term, ids in hit_ids.items():
        for doc_id in ids[: knowledge.DISCOVERY_SAMPLE_SIZE]:
            if not budget.allows():
                budget.exceeded = True
                return fields, mime_types, extensions, docs_sampled
            if doc_id not in props_cache:
                budget.record()
                props_cache[doc_id] = get_document_props(sid, doc_id)
            props = props_cache[doc_id]
            if props is None:
                continue
            docs_sampled += 1
            _inspect_props(term, doc_id, props, fields, mime_types, extensions)
    return fields, mime_types, extensions, docs_sampled


def _finalize_fields(fields_raw: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Projeta o histograma bruto no shape §10.3 (só nomes + cobertura).

    Args:
        fields_raw: acumuladores internos de ``_collect_samples``.

    Returns:
        ``{campo: {docs_na_amostra, probes_que_o_veem[:3], value_type}}``
        com ``value_type`` ``mixed`` quando o campo já apareceu str e list.
    """
    out: dict[str, Any] = {}
    for key, entry in fields_raw.items():
        types = entry["types"]
        value_type = next(iter(types)) if len(types) == 1 else _TYPE_MIXED
        out[key] = {
            "docs_na_amostra": len(entry["docs"]),
            "probes_que_o_veem": list(entry["probes"]),
            "value_type": value_type,
        }
    return out


def _probe_regex_detectors(
    sid: str, budget: _Budget
) -> tuple[dict[str, int], list[str]]:
    """Sonda a disponibilidade dos 9 detectores canônicos (§10.2.3).

    Args:
        sid: source roteado.
        budget: orçamento de GETs da chamada.

    Returns:
        ``(regex_availability, warnings)`` com ``{N: hits}`` (-1 = query que
        falhou, convenção ``query_hits``); warning nominal quando NENHUM
        detector respondeu hits (source sem varredura Regex — o "0
        silencioso" nunca é mudo).
    """
    availability: dict[str, int] = {}
    for name in KNOWN_TYPES:
        if not budget.allows():
            budget.exceeded = True
            break
        budget.record()
        try:
            availability[name] = len(
                search_ids(build_regex_query(name), sid, hint=_SEARCH_HINT)
            )
        except ValueError:
            availability[name] = _REGEX_FAILED_HITS
    warnings = []
    if availability and all(hits <= 0 for hits in availability.values()):
        warnings.append(knowledge.DISCOVERY_NO_REGEX_DETECTOR_WARNING)
    return availability, warnings


def _fresh_build(sid: str, budget: _Budget) -> _Build:
    """Constrói o catálogo do zero: probes → amostra → sondas Regex.

    Args:
        sid: source roteado.
        budget: orçamento de GETs da chamada (compartilhado pelas 3 fases).

    Returns:
        ``_Build`` com payload §10.3 (+ ``_warnings``/``_docs_sampled``),
        pares inspecionados e a marca de orçamento estourado.
    """
    warnings: list[str] = [knowledge.DISCOVERY_REGEX_SONDA_WARNING]
    probes, hit_ids, probe_warnings = _run_probes(sid, budget)
    warnings.extend(probe_warnings)
    if not budget.exceeded:
        fields_raw, mime_types, extensions, docs_sampled = _collect_samples(
            sid, hit_ids, budget
        )
    else:
        fields_raw, mime_types, extensions, docs_sampled = {}, {}, {}, 0
    regex_availability, regex_warnings = (
        ({}, []) if budget.exceeded else _probe_regex_detectors(sid, budget)
    )
    warnings.extend(regex_warnings)
    payload: dict[str, Any] = {
        "probes": probes,
        "fields": _finalize_fields(fields_raw),
        "regex_availability": regex_availability,
        "mime_types": mime_types,
        "extensions": extensions,
        "sample_size": knowledge.DISCOVERY_SAMPLE_SIZE,
        "_warnings": warnings,
        "_docs_sampled": docs_sampled,
    }
    return _Build(payload=payload, docs_sampled=docs_sampled, exceeded=budget.exceeded)


def _cache_get(sid: str, now: float) -> _CacheEntry | None:
    """Entrada viva do cache do source; None ausente/expirada.

    Args:
        sid: source consultado.
        now: ``time.monotonic()`` de referência para o TTL.

    Returns:
        A entrada (payload PROFUNDO-copiado pelo chamador) ou None.
    """
    with _CACHE_LOCK:
        entry = _CACHE.get(sid)
        if entry is None:
            return None
        if now - entry.built_at >= knowledge.DISCOVERY_CACHE_TTL:
            _CACHE.pop(sid, None)
            return None
        return entry


def _cache_set(sid: str, payload: dict[str, Any], now: float) -> None:
    """Publica o catálogo completo no cache (parcial NUNCA é cacheado).

    Args:
        sid: source do catálogo.
        payload: payload completo do build (sem truncamento de orçamento).
        now: ``time.monotonic()`` da publicação.

    Returns:
        Nada.
    """
    with _CACHE_LOCK:
        _CACHE[sid] = _CacheEntry(built_at=now, payload=payload)


def _is_empty(probes: dict[str, Any], fields: dict[str, Any],
              regex_availability: dict[str, int]) -> bool:
    """Diz se o catálogo inteiro não encontrou evidência (mensagem §10.5).

    Args:
        probes: resultados dos probes de frase.
        fields: histograma de campos.
        regex_availability: contagem dos detectores.

    Returns:
        True quando nenhum probe achou hit, nenhum campo foi amostrado e
        nenhum detector respondeu hits.
    """
    has_hits = any(p.get("total_hits") for p in probes.values())
    has_regex = any(hits > 0 for hits in regex_availability.values())
    return not has_hits and not fields and not has_regex


def _envelope(
    sid: str,
    started: float,
    payload: dict[str, Any],
    cache_info: dict[str, Any],
    budget: _Budget,
    docs_sampled: int,
) -> dict[str, Any]:
    """Monta o envelope T-10 (§10.3): catálogo + cache + counters + avisos.

    Args:
        sid: source triado.
        started: ``time.monotonic()`` do início da chamada.
        payload: catálogo (cache-hit já copiado; mutado localmente via dict).
        cache_info: ``{served_from_cache, ttl_s, age_s}``.
        budget: orçamento da chamada (0 GETs num cache-hit).
        docs_sampled: pares (probe, doc) do build servido.

    Returns:
        Envelope completo com ``message`` quando o catálogo está vazio.
    """
    body = dict(payload)
    warnings: list[str] = list(body.pop("_warnings", []))
    body.pop("_docs_sampled", None)
    served = bool(cache_info["served_from_cache"])
    if served:
        warnings.append(knowledge.DISCOVERY_CACHE_WARNING)
    elapsed = max(time.monotonic() - started, 0.0)
    if elapsed > knowledge.DISCOVERY_SLOW_WARNING_S and not served:
        warnings.append(
            knowledge.DISCOVERY_SLOW_WARNING_TEMPLATE.format(
                elapsed=elapsed, threshold=knowledge.DISCOVERY_SLOW_WARNING_S
            )
        )
    if budget.exceeded:
        warnings.append(
            knowledge.HTTP_BUDGET_WARNING_TEMPLATE.format(
                calls=budget.used, cap=budget.cap, omitted=_BUDGET_OMITTED
            )
        )
    counters = dict(
        op_envelope(
            started, scanned=docs_sampled, processed=len(body["fields"])
        ),
        probes_ran=len(body["probes"]),
        docs_sampled=docs_sampled,
        http_calls=budget.used,
        cached=served,
    )
    if budget.exceeded:
        counters["phase"] = progress.PHASE_PARTIAL
    envelope: dict[str, Any] = {
        "source_id": sid,
        **body,
        "cache": cache_info,
        "counters": counters,
        "truncated": {"http_budget_exceeded": budget.exceeded},
        "warnings": warnings,
    }
    if _is_empty(body["probes"], body["fields"], body["regex_availability"]):
        envelope["message"] = knowledge.DISCOVERY_EMPTY_MESSAGE
    return envelope


def build_triage_query_discovery(source_id: str) -> dict[str, Any]:
    """Núcleo da descoberta de superfície do source (lógica testável).

    Args:
        source_id: source a descobrir (obrigatório).

    Returns:
        Envelope §10.3 (shape documentado em ``triage_query_discovery``).

    Raises:
        ValueError: source_id vazio, inexistente ou endpoint offline
            (textos estáveis da política M-2, via ``Router.resolve``).
    """
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError(_EMPTY_SID_MESSAGE)
    get_router().resolve(sid)  # M-2 antes de qualquer HTTP (§0.1)
    started = time.monotonic()
    budget = _Budget(knowledge.TOOL_BUDGETS["triage_query_discovery"])
    entry = _cache_get(sid, started)
    if entry is not None:
        cache_info = {
            "served_from_cache": True,
            "ttl_s": knowledge.DISCOVERY_CACHE_TTL,
            "age_s": started - entry.built_at,
        }
        return _envelope(
            sid, started, copy.deepcopy(entry.payload), cache_info, budget,
            entry.payload["_docs_sampled"],
        )
    build = _fresh_build(sid, budget)
    if not build.exceeded:
        _cache_set(sid, build.payload, time.monotonic())
    cache_info = {
        "served_from_cache": False,
        "ttl_s": knowledge.DISCOVERY_CACHE_TTL,
        "age_s": None,
    }
    return _envelope(sid, started, build.payload, cache_info, budget,
                     build.docs_sampled)


def triage_query_discovery(source_id: str) -> dict[str, Any]:
    """Descobre o QUE EXISTE num source: catálogo por evidência, com cache.

    Roda 33 probes de frase LUCENE (lista canônica do ICARO em
    ``knowledge.DISCOVERY_PROBES`` — frases soltas, nunca regex do usuário),
    amostra até ``DISCOVERY_SAMPLE_SIZE`` docs de cada probe com hits e
    devolve o histograma de NOMES de campos com cobertura (docs que o têm e
    até 3 probes que o veem, ``value_type`` str/list/mixed), buckets de
    mime/extensão da amostra e a disponibilidade dos 9 detectores
    ``Regex:<N>`` canônicos do servidor (sonda = KNOWN_TYPES; o
    RegexConfig.txt completo é externo ao MCP — warning nominal documenta).
    Pin de privacidade: nenhum VALOR de conteúdo atravessa — só nomes e
    contagens. Falha de probe (500/timeout) marca ``available: false`` +
    ``error`` e segue (fiel ao ICARO :338-353, nunca aborta).

    O catálogo fica em cache por source (TTL ``DISCOVERY_CACHE_TTL``): a
    2ª chamada na janela serve ``cache.served_from_cache=true`` com
    ``counters.http_calls=0`` e warning ``catalogo_servido_de_cache``
    (R-TRIAGE-1: cache não conta GET). Builds truncados por orçamento
    nunca são cacheados. Probes de frase são queries de conteúdo caras —
    elapsed > ``DISCOVERY_SLOW_WARNING_S`` emite warning sugerindo o reuso
    do cache (o agente decide re-chamar só pelo bloco ``fields``).

    Args:
        source_id: source a descobrir (veja iped_list_sources).

    Returns:
        {source_id, probes {termo: {total_hits, available, error?}}, fields
        {campo: {docs_na_amostra, probes_que_o_veem[:3], value_type}},
        regex_availability {N: hits} (-1 = query que falhou), mime_types
        {mime: contagem}, extensions {ext: contagem}, sample_size,
        cache {served_from_cache, ttl_s, age_s?}, counters (op_envelope +
        probes_ran/docs_sampled/http_calls/cached), truncated
        (http_budget_exceeded), warnings}; ``message`` quando o catálogo
        está vazio (nunca exceção).

    Raises:
        ValueError: source_id vazio, inexistente ou servidor indisponível
            (textos estáveis da política M-2).
    """
    return build_triage_query_discovery(source_id)


def register(mcp: FastMCP) -> None:
    """Registra a tool de query discovery (T-10) no servidor.

    Args:
        mcp: servidor FastMCP ativo.

    Returns:
        Nada.
    """
    mcp.add_tool(triage_query_discovery)
