"""Tool ``triage_media`` — eixo mídia (T-6, DESIGN 09-B §6).

Porta o motor de ``media_strategy.py``: coleta por categoria (gate
``any(h in cat ...)`` :73-74 com :data:`knowledge.MEDIA_CATEGORIES`), chat
detectado por nome/contentType (:70-72) e itens RANQUEADOS por 12
preditores de sinal (:28-46) com base 40 chat / 50 mídia (:77) — o eixo
entrega *itens*, não entidades (:91-92). Agregados do
``_media_owner_summary`` (draft_report:1500-1537): contagem por categoria
crua, apps detectados e contas/titulares vistos em mídia (teto 20).
Relevância do achado pelo handler ``_finding_from_ranked_item``
(draft_report:963-975: ALTA >= 85, MÉDIA >= 60, senão BAIXA).

A política de poda ``MEDIA_DROP_KEYS``/``MEDIA_MAX_VAL``
(draft_report:75-77, uso :798-806) era do payload do prompt ICARO; aqui ela
assume o papel dado pelo design §6.2: excluir chaves de blob/binário do
histograma de NOMES de campos publicado (``counters.field_hits``). O
envelope jamais traz valores de propriedades — nomes apenas (precedente de
privacidade do T-10).

Divergências de fonte registradas no relatório da fase (D-10/D-11): o
design §6.1/§6.3 descreve categorias acentuadas e chaves de dono
("Author"...) que NÃO existem na cópia real do ICARO — a fonte manda
(10 categorias, 5 chaves de dono). Quirks fiéis pinados em teste:
"registrato" (:43), ``_true`` só casa "true" (:58-63 — NÃO "sim"/"1"),
classeNudez como string BR (:31/:48-56). Nenhum LLM, nenhum arquivo do
caso: só ``GET /search``/``GET /docs/{id}`` com orçamento R-TRIAGE-1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from iped_mcp.fetch import search_ids
from iped_mcp.progress import PHASE_PARTIAL, op_envelope
from iped_mcp.router import get_router
from iped_mcp.triage import knowledge
from iped_mcp.triage.fields import first_str
from iped_mcp.triage.budget import (
    HttpBudget as _Budget,
    budget_warnings as _budget_warnings,
    fetch_within_budget as _fetch_within_budget,
)

_SEARCH_HINT = (
    "confira source_id (iped_list_sources); categorias do eixo podem existir "
    "com outro rótulo no caso (lista canônica: knowledge.MEDIA_CATEGORIES)."
)
_EMPTY_SID_MESSAGE = "source_id é obrigatório e não pode ser vazio."
_GPS_SIGNAL = next(s for s in knowledge.MEDIA_SIGNALS if s[0] == "GPS")


@dataclass
class _Scan:
    """Estado da varredura T-6 (queries, caps, contadores, avisos)."""

    query_hits: dict[str, int] = field(default_factory=dict)
    query_caps: dict[str, int] = field(default_factory=dict)
    total_cap_applied: bool = False
    docs_searched: int = 0
    docs_fetched: int = 0
    itens_chats: int = 0
    itens_midias: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Agg:
    """Agregados do eixo (``_media_owner_summary``, draft_report:1500-1537)."""

    by_category: dict[str, int] = field(default_factory=dict)
    apps: dict[str, int] = field(default_factory=dict)
    owner_signals: list[str] = field(default_factory=list)
    field_hits: dict[str, int] = field(default_factory=dict)


# --- preditores (media_strategy.py:28-46; helpers :48-63) ----------------------


def _num(props: dict[str, Any], key: str) -> int:
    """Número BR da propriedade — porta ``MediaStrategy._num`` (:48-56).

    Args:
        props: propriedades do doc no envelope do jar (valores em lista).
        key: chave da propriedade.

    Returns:
        ``int(float(v))`` após remover "." de milhar e trocar "," decimal
        (literal :54); 0 para ausente/não numérico (:55-56). Quirk fiel:
        "2.0" vira 20 (o ponto é sempre milhar na fonte).
    """
    value = props.get(key)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return int(float(str(value or "0").replace(".", "").replace(",", ".")))
    except ValueError:
        return 0


def _flag_true(props: dict[str, Any], key: str) -> bool:
    """Porta ``MediaStrategy._true`` (:58-63): só ``"true"`` casa.

    Args:
        props: propriedades do doc.
        key: chave da propriedade.

    Returns:
        ``True`` somente quando o valor, stripado e minúsculo, é exatamente
        ``"true"`` — DIVERGÊNCIA DO DESIGN §6.2 ("sim"/"1"/"yes" NÃO casam;
        a fonte :63 confere só ``== "true"``, quirk pinado em teste).
    """
    value = props.get(key)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return str(value or "").strip().lower() == "true"


def _present(props: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """Alguma das chaves tem valor não vazio (sinais bool da fonte).

    Args:
        props: propriedades do doc.
        keys: chaves candidatas (OR da fonte, ex.: URL/GPS :35-37).

    Returns:
        ``True`` quando alguma chave produz valor não vazio via
        ``fields.first_str``. Leitura levemente mais estrita que o
        ``bool(p.get(...))`` do ICARO (``[""]`` casaria lá); registrada no
        docstring do motor.
    """
    return any(first_str(props, (key,)) for key in keys)


def _download_financeiro(props: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """URL de download financeiro por substring (media_strategy.py:42-45).

    Args:
        props: propriedades do doc.
        keys: tupla de 1 chave (``ufed:URL``) — assinatura unificada dos
            avaliadores.

    Returns:
        ``True`` quando ``MEDIA_DOWNLOAD_URL_MARKERS`` aparece na URL
        minúscula (inclui o quirk literal "registrato" :43).
    """
    url = first_str(props, keys).lower()
    return any(marker in url for marker in knowledge.MEDIA_DOWNLOAD_URL_MARKERS)


_SIGNAL_EVALUATORS: dict[str, Callable[[dict[str, Any], tuple[str, ...]], bool]] = {
    "num_positive": lambda p, k: any(_num(p, key) > 0 for key in k),
    "num_at_least_2": lambda p, k: any(_num(p, key) >= 2 for key in k),
    "present": _present,
    "flag_true": lambda p, k: any(_flag_true(p, key) for key in k),
    "download_url": _download_financeiro,
}


def _signal_hits(props: dict[str, Any]) -> list[tuple[str, int]]:
    """Sinais encontrados como ``(nome, pontos)`` na ordem da fonte (:76).

    Args:
        props: propriedades do doc.

    Returns:
        Uma entrada por preditor verdadeiro; a ordem é a de
        ``MEDIA_SIGNALS`` (= ordem de exibição da justificação :81).
    """
    return [
        (name, points)
        for name, keys, points, evaluator_key in knowledge.MEDIA_SIGNALS
        if _SIGNAL_EVALUATORS[evaluator_key](props, keys)
    ]


# --- gate, chat e score (media_strategy.py:70-81) ------------------------------


def _is_chat(category: str, name: str, content_type: str) -> bool:
    """Chat = whatsapp na categoria E "chat" no nome, OU contentType (:70-72).

    Args:
        category: categoria minúscula do doc.
        name: nome do doc.
        content_type: valor cru da propriedade ``contentType``.

    Returns:
        ``True`` quando o item entra como chat-base (:71-72).
    """
    lowered_type = content_type.lower()
    marked_type = any(
        marker in lowered_type for marker in knowledge.MEDIA_CHAT_CONTENT_TYPES
    )
    chat_named = (
        knowledge.MEDIA_CHAT_CATEGORY_MARKER in category
        and knowledge.MEDIA_CHAT_NAME_MARKER in name.lower()
    )
    return chat_named or marked_type


def _accepted(category: str, is_chat: bool) -> bool:
    """Gate do eixo (:73-74): chat entra sempre; resto exige hint de mídia.

    Args:
        category: categoria minúscula do doc.
        is_chat: resultado de ``_is_chat``.

    Returns:
        ``True`` quando o item é aceito no ranqueamento (texto puro de
        termos genéricos fica de fora, comentário :74 da fonte).
    """
    return is_chat or any(
        hint in category for hint in knowledge.MEDIA_CATEGORIES
    )


def _score_and_justification(
    is_chat: bool, hits: list[tuple[str, int]]
) -> tuple[int, str]:
    """Base + soma dos pontos, com justificação literal (:77-81).

    Args:
        is_chat: item classificado como chat.
        hits: ``(nome, pontos)`` de ``_signal_hits``.

    Returns:
        ``(score, justificativa)`` — chat recebe
        ``MEDIA_JUSTIFICATION_CHAT`` mesmo com sinais (fiel :80).
    """
    base = knowledge.MEDIA_BASE_CHAT if is_chat else knowledge.MEDIA_BASE_MEDIA
    score = base + sum(points for _, points in hits)
    if is_chat:
        return score, knowledge.MEDIA_JUSTIFICATION_CHAT
    if not hits:
        return score, knowledge.MEDIA_JUSTIFICATION_NO_SIGNALS
    names = ", ".join(name for name, _ in hits)
    return score, knowledge.MEDIA_JUSTIFICATION_PREFIX + names


def _media_relevance(score: int) -> str:
    """Relevância do achado (draft_report:965, comparação INCLUSIVA).

    Args:
        score: score do item ranqueado.

    Returns:
        ALTA >= 85, MÉDIA >= 60, senão BAIXA. Diferente dos limiares
        estritos de telefonia/geo: aqui 85 e 60 SÃO ALTA/MÉDIA (:965).
    """
    if score >= knowledge.MEDIA_RELEVANCE_ALTA_MIN:
        return knowledge.RELEVANCE_ALTA
    if score >= knowledge.MEDIA_RELEVANCE_MEDIA_MIN:
        return knowledge.RELEVANCE_MEDIA
    return knowledge.RELEVANCE_BAIXA


def _rank_item(doc_id: int, props: dict[str, Any]) -> dict[str, Any] | None:
    """Doc → item ranqueado do envelope; ``None`` se o gate recusa (:67-87).

    Args:
        doc_id: id do documento.
        props: propriedades do doc.

    Returns:
        ``{doc_id, name, category, path, score, relevance, signals,
        justification, is_chat}`` (shape §6.5; ``is_chat`` alimenta as
        contagens e sai do item publicado) — ``None`` quando ``_accepted``
        recusa (texto puro sem hint de mídia).
    """
    category = first_str(props, (knowledge.CATEGORY_FIELD,))
    name = first_str(props, (knowledge.NAME_FIELD,))
    is_chat = _is_chat(
        category.lower(), name, first_str(props, (knowledge.MEDIA_CONTENT_TYPE_FIELD,))
    )
    if not _accepted(category.lower(), is_chat):
        return None
    hits = _signal_hits(props)
    score, justification = _score_and_justification(is_chat, hits)
    return {
        "doc_id": doc_id,
        "name": name,
        "category": category,
        "path": first_str(props, (knowledge.PATH_FIELD,)),
        "score": score,
        "relevance": _media_relevance(score),
        "signals": [name for name, _ in hits],
        "justification": justification,
        "is_chat": is_chat,
    }


# --- agregados (_media_owner_summary, draft_report:1500-1537) ------------------


def _accumulate(agg: _Agg, item: dict[str, Any], props: dict[str, Any]) -> None:
    """Soma categoria crua, apps e sinais de dono de um item aceito.

    Args:
        agg: agregados da varredura (mutado).
        item: item ranqueado de ``_rank_item``.
        props: propriedades do doc (para ServiceIdentifier e chaves de dono).
    """
    category = item["category"] or knowledge.MEDIA_NOT_IDENTIFIED_VALUE  # :1509
    agg.by_category[category] = agg.by_category.get(category, 0) + 1
    _accumulate_apps(agg, item, props)
    _accumulate_owner(agg, props)


def _accumulate_apps(
    agg: _Agg, item: dict[str, Any], props: dict[str, Any]
) -> None:
    """Apps detectados: marcador no path e ``ufed:ServiceIdentifier`` (:1519-1524).

    Args:
        agg: agregados (mutado).
        item: item ranqueado (usa ``path``).
        props: propriedades do doc.
    """
    if knowledge.MEDIA_WHATSAPP_PATH_MARKER in item["path"].lower():
        label = knowledge.MEDIA_WHATSAPP_APP_LABEL
        agg.apps[label] = agg.apps.get(label, 0) + 1
    service = first_str(props, (knowledge.MEDIA_SERVICE_IDENTIFIER_FIELD,))
    if service:
        agg.apps[service] = agg.apps.get(service, 0) + 1


def _accumulate_owner(agg: _Agg, props: dict[str, Any]) -> None:
    """Contas/titulares vistos em mídia: dedup, "N/I" fora, teto 20 (:1525-1528).

    Args:
        agg: agregados (mutado).
        props: propriedades do doc.
    """
    if len(agg.owner_signals) >= knowledge.MEDIA_OWNER_SIGNALS_MAX:
        return
    for key in knowledge.MEDIA_OWNER_SIGNAL_KEYS:
        value = first_str(props, (key,))
        if not value or value.upper() == knowledge.MEDIA_NOT_IDENTIFIED_VALUE:
            continue
        if value not in agg.owner_signals:
            agg.owner_signals.append(value)
            if len(agg.owner_signals) >= knowledge.MEDIA_OWNER_SIGNALS_MAX:
                return


def _accumulate_field_hits(agg: _Agg, props: dict[str, Any]) -> None:
    """Histograma de NOMES de campos com a poda ``MEDIA_DROP_KEYS`` (§6.2).

    Args:
        agg: agregados (mutado).
        props: propriedades do doc.

    Returns:
        Nada.
    """
    for key in props:
        if any(dropped in key.lower() for dropped in knowledge.MEDIA_DROP_KEYS):
            continue
        agg.field_hits[key] = agg.field_hits.get(key, 0) + 1


# --- coleta de candidatos (§0.2; caps herdados de base_strategy) ---------------


def _collect_candidates(sid: str, scan: _Scan, budget: _Budget) -> list[int]:
    """Executa as queries de categoria e deduplica candidatos.

    Args:
        sid: source_id resolvido.
        scan: estado da varredura (mutado; falha 500 vira ``-1`` + warning
            nominal, M-2/M-3 — o eixo continua).
        budget: orçamento de GET (mutado; cada /search conta 1).

    Returns:
        Ids únicos na ordem de descoberta com os caps por query (:2500) e
        global (:5000) aplicados e marcados.
    """
    out: list[int] = []
    seen: set[int] = set()
    for category in knowledge.MEDIA_CATEGORIES:
        if not budget.allows():
            budget.exceeded = True
            break
        query = knowledge.TELEPHONY_QUERY_TEMPLATE.format(category=category)
        try:
            ids = search_ids(query, sid, hint=_SEARCH_HINT)
        except ValueError as exc:
            scan.query_hits[query] = -1
            scan.warnings.append(f"query {query} falhou: {exc}")
            budget.record()
            continue
        budget.record()
        scan.query_hits[query] = len(ids)
        if len(ids) > knowledge.CATEGORY_QUERY_CAP:
            scan.query_caps[query] = len(ids) - knowledge.CATEGORY_QUERY_CAP
            ids = ids[: knowledge.CATEGORY_QUERY_CAP]
        for doc_id in ids:
            if doc_id in seen:
                continue
            seen.add(doc_id)
            out.append(doc_id)
            if len(out) >= knowledge.TOTAL_CANDIDATE_CAP:
                scan.total_cap_applied = True
                return out
    return out


# --- envelope e tool (DESIGN 09-B §6.5) -----------------------------------------


def _truncated_payload(
    items: list[dict[str, Any]],
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
    field_hits_omitted: int,
) -> dict[str, Any]:
    """Bloco ``truncated``: tetos SEMPRE marcados com omissões (§6.5)."""
    return {
        "total_cap_applied": scan.total_cap_applied,
        "query_caps_applied": list(scan.query_caps),
        "items_truncated": len(items) > knowledge.MEDIA_ITEMS_MAX,
        "items_omitted": max(len(items) - knowledge.MEDIA_ITEMS_MAX, 0),
        "field_hits_truncated": field_hits_omitted > 0,
        "field_hits_omitted": field_hits_omitted,
        "http_budget_exceeded": budget.exceeded,
        "http_budget_calls_at_stop": budget.used if budget.exceeded else None,
        # docs_omitted carrega a QUANTIDADE num único elemento
        # (contrato _fetch_within_budget: docs_omitted[0],
        # identity.py:1204-1206) — por isso sum(), não len().
        "docs_omitted_by_budget": sum(docs_omitted),
    }


def _top_field_hits(agg: _Agg) -> tuple[dict[str, int], int]:
    """Top-N do histograma de campos (por contagem desc, nome como empate).

    Args:
        agg: agregados da varredura.

    Returns:
        ``(histograma[:40], omissões)`` — nomes e contagens apenas; valores
        jamais publicados (pino de privacidade herdado do T-10).
    """
    ordered = sorted(agg.field_hits.items(), key=lambda kv: (-kv[1], kv[0]))
    top = dict(ordered[: knowledge.MEDIA_FIELD_HITS_TOP])
    return top, len(ordered) - len(top)


def _gps_coverage_warning(items: list[dict[str, Any]], scan: _Scan) -> None:
    """Warning de cobertura GPS 0 em mídia varrida (TODO :36-37, M-7).

    Args:
        items: itens ranqueados aceitos.
        scan: estado da varredura (mutado).

    Returns:
        Nada.
    """
    if scan.itens_midias == 0:
        return
    if any("GPS" in item["signals"] for item in items):
        return
    keys = "/".join(_GPS_SIGNAL[1])
    scan.warnings.append(knowledge.MEDIA_GPS_ZERO_WARNING.format(keys=keys))


def _payload(
    sid: str,
    started: float,
    items: list[dict[str, Any]],
    agg: _Agg,
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
) -> dict[str, Any]:
    """Envelope §6.5 com op_envelope de M-9 + contadores do eixo."""
    scan.warnings.extend(_budget_warnings(budget, docs_omitted))
    _gps_coverage_warning(items, scan)
    field_hits, field_hits_omitted = _top_field_hits(agg)
    counters = dict(
        op_envelope(started, scanned=scan.docs_searched, processed=len(items))
    )
    counters.update(
        docs_searched=scan.docs_searched,
        docs_fetched=scan.docs_fetched,
        http_calls=budget.used,
        cached=False,
        field_hits=field_hits,
    )
    if budget.exceeded:
        counters["phase"] = PHASE_PARTIAL
    return {
        "source_id": sid,
        "ranked_items": items[: knowledge.MEDIA_ITEMS_MAX],
        "items_total": len(items),
        "itens_chats": scan.itens_chats,
        "itens_midias": scan.itens_midias,
        "by_category": agg.by_category,
        "apps": agg.apps,
        "owner_media_signals": agg.owner_signals,
        "query_hits": scan.query_hits,
        "counters": counters,
        "truncated": _truncated_payload(
            items, scan, budget, docs_omitted, field_hits_omitted
        ),
        "warnings": scan.warnings,
        **({"message": knowledge.MEDIA_EMPTY_MESSAGE} if not items else {}),
    }


def build_triage_media(source_id: str) -> dict[str, Any]:
    """Executa a triagem de mídia (motor T-6; ver ``triage_media``).

    Args:
        source_id: source resolvido no Router (erros M-2 viram ValueError).

    Returns:
        Envelope do payload §6.5 da chamada corrente.
    """
    sid = (source_id or "").strip()
    if not sid:
        raise ValueError(_EMPTY_SID_MESSAGE)
    get_router().resolve(sid)
    started = time.monotonic()
    scan = _Scan()
    agg = _Agg()
    budget = _Budget(knowledge.TOOL_BUDGETS["triage_media"])
    ids = _collect_candidates(sid, scan, budget)
    scan.docs_searched = len(ids)
    omitted: list[int] = []
    pairs = _fetch_within_budget(sid, ids, budget, omitted)
    scan.docs_fetched = sum(1 for _, props in pairs if props is not None)
    items = _rank_documents(pairs, scan, agg)
    return _payload(sid, started, items, agg, scan, budget, omitted)


def _rank_documents(
    pairs: list[tuple[int, dict[str, Any] | None]], scan: _Scan, agg: _Agg
) -> list[dict[str, Any]]:
    """Ranqueia os docs aceitos e acumula agregados (ordem estável :88).

    Args:
        pairs: ``(doc_id, props|None)`` da varredura (None = doc sumiu).
        scan: estado da varredura (mutado; contagens chat/mídia).
        agg: agregados (mutado).

    Returns:
        Itens aceitos ordenados por score desc (sort estável da fonte :88;
        empate preserva a ordem de descoberta).
    """
    items: list[dict[str, Any]] = []
    for doc_id, props in pairs:
        if props is None:
            continue
        _accumulate_field_hits(agg, props)
        item = _rank_item(doc_id, props)
        if item is None:
            continue
        if item.pop("is_chat"):
            scan.itens_chats += 1
        else:
            scan.itens_midias += 1
        _accumulate(agg, item, props)
        items.append(item)
    items.sort(key=lambda item: -item["score"])
    return items


def triage_media(source_id: str) -> dict[str, Any]:
    """Triagem de mídia do ICARO (T-6): itens ranqueados por sinais.

    Args:
        source_id: id do source no servidor WebAPI do caso.

    Returns:
        Envelope ``{source_id, ranked_items[:50] + items_total,
        by_category, apps, owner_media_signals[:20], query_hits, counters
        (+field_hits), truncated, warnings[, message]}`` — cada item com
        ``score`` (base 50 mídia / 40 chat + soma dos 12 sinais
        OCR/rosto/nudez/transcrito/CPF/telefone/URL/GPS/ext-divergente/
        carved/deletado/download-financeiro), ``relevance`` por score
        (ALTA >= 85, MÉDIA >= 60, BAIXA), ``signals`` e justificativa
        literal do ICARO. Contagem por categoria crua, apps (marcador
        ``com.whatsapp`` no path + ``ufed:ServiceIdentifier``) e
        contas/titulares vistos em mídia com "N/I" ignorado. O histograma
        ``counters.field_hits`` publica NOMES de campos (teto 40) com a poda
        ``MEDIA_DROP`` do ICARO — valores jamais.

    Raises:
        ToolError: source inexistente/servidor offline (M-2) ou erro
            inesperado traduzido; estouro de orçamento NUNCA raising — vira
            ``truncated.http_budget_exceeded`` + warning.
    """
    try:
        return build_triage_media(source_id)
    except ValueError:
        raise
    except Exception as exc:  # traduz erro inesperado (padrão da família)
        raise ToolError(f"triage_media falhou: {exc}") from exc


def register(mcp: FastMCP) -> None:
    """Registra a tool de triagem de mídia (triage_media) no servidor.

    Args:
        mcp: servidor FastMCP ativo.

    Returns:
        Nada.
    """
    mcp.add_tool(triage_media)
