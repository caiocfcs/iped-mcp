"""Tool ``triage_communication`` — eixo comunicação (T-8, DESIGN 09-B §8).

Porta o ``communication_strategy`` do ICARO em modo agregada-v1 (D-9): para
cada interlocutor visível publica contagens e pontuação — NUNCA o texto das
mensagens. Três fontes alimentam a varredura (§8.2):

1. ``category:"<cat>"`` para ``COMMUNICATION_CATEGORIES`` (12 entradas,
   communication_strategy.py:241-245);
2. os builders canônicos de WhatsApp (``whatsapp/queries.py``) — IMPORTADOS,
   jamais replicados (MANDATO DEDUP §8.0; lição M-18) — para garantir
   cobertura WA sem depender da categoria;
3. o mesmo lote de docs alimenta o histórico de busca: gate
   ``COMMUNICATION_BROWSERS`` sobre ``ufed:Source`` minúsculo (:689), query
   ``ufed:Value`` não vazio (:394-397), timestamp ``ufed:TimeStamp`` senão
   ``"N/I"`` (:686).

Agregação por chave visível (``Communication:To`` normalizado como no ICARO
:470-484/:513-521, senão ``ufed:ChatId`` sem mapa, senão DESCONHECIDO) + logs
de interação ``InteractionC`` parseados pelo regex literal do ICARO (:408,
constante ``COMMUNICATION_INTERACTION_BODY_RE``). A mescla por chat-id
legacy/current (:427-660) depende de estado rico (mapa chat_id→nome) e NÃO é
portada — declarado no warning ``agregada-v1`` (D-9).

Pontuação fiel ao handler ``_finding_from_communication``
(draft_report_iped_manager.py:866-869): ``total = max(chat_total, count)``;
com termos ``min(50+total, 95)``, sem termos ``min(40+total//2, 70)``;
relevância ALTA com termos, senão MÉDIA se ``total > 50`` (ESTRITO), senão
BAIXA. Histórico: score 40 fixo, BAIXA, sort temporal desc + [:50]
(:742-743; handler draft_report:950-960). Os termos investigativos são o
parâmetro ``search_terms`` (EXCEÇÃO única ao "só source_id", §8.1: no ICARO
vêm do YAML ``content_terms.operational``, ausente na cópia). Erros M-2,
envelope M-9 e orçamento R-TRIAGE-1 (3000, §8.7) como no resto da família.
Nenhum LLM, nenhum arquivo do caso: só ``GET /search``/``GET /docs/{id}``.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

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
from iped_mcp.whatsapp import queries as whatsapp_queries

_SEARCH_HINT = (
    "confira source_id (iped_list_sources); o eixo consulta as categorias de "
    "knowledge.COMMUNICATION_CATEGORIES e os builders canônicos de WhatsApp."
)
_EMPTY_SID_MESSAGE = "source_id é obrigatório e não pode ser vazio."

# Regex compiladas das constantes literais do strategy (:408/:475/:515).
_RE_INTERACTION_BODY = re.compile(knowledge.COMMUNICATION_INTERACTION_BODY_RE)
_RE_TO = re.compile(knowledge.COMMUNICATION_TO_RE)
_RE_EMAIL_ANGLE = re.compile(knowledge.COMMUNICATION_EMAIL_ANGLE_RE)

# Marcador de id interno em Communication:To de grupo ("Nome (id:...)") :473.
_GROUP_ID_MARKER = "(id:"
# Sufixo JID de grupo nos logs InteractionC (:412).
_GROUP_JID_SUFFIX = "@g.us"
# Rótulo do grupo sem nome no log de interação (:413).
_WHATSAPP_GROUP_LABEL = "GRUPO WHATSAPP"


@dataclass
class _Scan:
    """Estado da varredura T-8 (queries, caps, contadores, avisos)."""

    query_hits: dict[str, int] = field(default_factory=dict)
    query_caps: list[str] = field(default_factory=list)
    scan_cap_applied: bool = False
    docs_searched: int = 0
    docs_fetched: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _ChatRecord:
    """Registro por interlocutor visível (espelho CommunicationRecord)."""

    interlocutor: str
    is_group: bool
    message_count: int = 0
    chat_total_messages: int = 0
    terms: list[str] = field(default_factory=list)


# --- queries e coleta (DESIGN 09-B §8.2/§8.4) -----------------------------------


def _communication_queries() -> tuple[str, ...]:
    """Queries do eixo: 12 categorias + builders WhatsApp canônicos (§8.2).

    Returns:
        Tupla ordenada sem duplicatas; a query de WhatsApp vem do builder
        ``whatsapp/queries.py`` (zero réplica literal, MANDATO DEDUP §8.0).
    """
    category_queries = [
        knowledge.TELEPHONY_QUERY_TEMPLATE.format(category=category)
        for category in knowledge.COMMUNICATION_CATEGORIES
    ]
    return tuple(
        dict.fromkeys([*category_queries, whatsapp_queries.build_all_wa_query()])
    )


def _collect_candidates(
    sid: str, queries: tuple[str, ...], scan: _Scan, budget: _Budget
) -> list[int]:
    """Executa as queries do eixo com dedup global, teto e orçamento.

    Args:
        sid: source já resolvido (M-2).
        queries: tupla de ``_communication_queries``.
        scan: contadores da varredura (mutado; query 500 vira hit -1 +
            warning nominal e o eixo continua, §0.1).
        budget: orçamento de GET (mutado; cada /search conta 1).

    Returns:
        ids únicos na ordem de descoberta, até ``CHAT_SCAN_CAP`` (§8.4; o
        corte marca ``scan.scan_cap_applied`` — chats reais têm dezenas de
        milhares de docs, varrer tudo não cabe no orçamento §2).
    """
    seen: dict[int, None] = {}
    for query in queries:
        if not budget.allows():
            budget.exceeded = True
            break
        try:
            ids = search_ids(query, sid, hint=_SEARCH_HINT)
        except ValueError as exc:
            budget.record()
            scan.warnings.append(f"query {query} falhou: {exc}")
            scan.query_hits[query] = -1
            continue
        budget.record()
        scan.query_hits[query] = len(ids)
        if len(ids) > knowledge.CATEGORY_QUERY_CAP:
            scan.query_caps.append(query)
            ids = ids[: knowledge.CATEGORY_QUERY_CAP]
        for doc_id in ids:
            if len(seen) >= knowledge.CHAT_SCAN_CAP:
                scan.scan_cap_applied = True
                return list(seen)
            seen[doc_id] = None
    return list(seen)


# --- aceite, dono e nomeação (communication_strategy.py) -------------------------


def _accepts(props: dict[str, Any], source: str) -> bool:
    """Gate de aceite do item (:252-257): categoria válida OU browser.

    Args:
        props: propriedades do doc.
        source: ``ufed:Source`` minúsculo.

    Returns:
        True quando o item pertence ao eixo comunicação.
    """
    category = first_str(props, (knowledge.CATEGORY_FIELD,)).lower()
    if any(cat in category for cat in knowledge.COMMUNICATION_CATEGORIES):
        return True
    return source in knowledge.COMMUNICATION_BROWSERS


def _owner_identifiers(pairs: list[tuple[int, dict | None]]) -> set[str]:
    """Identificadores do dono do aparelho (:304-315), minúsculos.

    Args:
        pairs: docs varridos ``(doc_id, properties|None)``.

    Returns:
        Conjunto de nome/telefone/id do dono (id sem o sufixo ``@...``),
        usado para vetar o próprio dono como interlocutor (:479-489).
    """
    owners: set[str] = set()
    for _, props in pairs:
        if not props:
            continue
        for owner_key in knowledge.COMMUNICATION_OWNER_FIELDS:
            value = first_str(props, (owner_key,))
            if value:
                owners.add(value.split("@")[0].lower())
    return owners


def _safe_int(raw: str) -> int:
    """Inteiro tolerante da contagem de mensagens (:443-445, exceto pass)."""
    try:
        return int(raw)
    except ValueError:
        return 0


def _normalize_display_name(name: str, is_group: bool, owners: set[str]) -> str:
    """Nome de exibição normalizado (:513-521), em maiúsculas.

    Args:
        name: candidato bruto (telefone entre parênteses é descartado;
            e-mail entre ``<...>`` é extraído).
        is_group: True para grupos (fallback ``GRUPO DESCONHECIDO``).
        owners: identificadores do dono (vetados como interlocutor).

    Returns:
        Nome limpo; ``DESCONHECIDO``/``GRUPO DESCONHECIDO`` quando vazio,
        curto (< 3 chars) ou pertencente ao dono.
    """
    display = name.split("(")[0].strip().upper()
    if "<" in display and ">" in display:
        email = _RE_EMAIL_ANGLE.search(display)
        if email:
            display = email.group(1).upper()
    if not display or len(display) < 3 or display.lower() in owners:
        display = (
            knowledge.COMMUNICATION_UNKNOWN_GROUP_LABEL
            if is_group
            else knowledge.COMMUNICATION_UNKNOWN_LABEL
        )
    return display


def _interlocutor_from_to(
    comm_to: str, is_group: bool, owners: set[str]
) -> str:
    """Candidato a interlocutor de ``Communication:To`` (:470-484).

    Args:
        comm_to: valor cru da propriedade (vazio = sem destinatário).
        is_group: flag lida de ``isGroupMessage``/``ufed:IsGroupMessage``.
        owners: identificadores do dono.

    Returns:
        Candidato bruto (ainda não normalizado); ``""`` quando nada resolve
        (nome/telefone pertencem ao dono — veto :479-482).
    """
    if not comm_to:
        return ""
    if is_group:
        return comm_to.split(_GROUP_ID_MARKER)[0].strip().upper()
    match = _RE_TO.match(comm_to)
    if match:
        name_part = match.group(1).strip()
        phone_part = match.group(2).strip()
        if name_part and name_part.lower() not in owners:
            return f"{name_part.upper()} ({phone_part})"
        if phone_part and phone_part.lower() not in owners:
            return phone_part.upper()
        return ""
    return comm_to.replace("~", "").strip().upper()


# --- agregação de chats e buscas (:390-425, :684-717) ----------------------------


def _find_investigative_hits(
    name: str, props: dict[str, Any], terms: tuple[str, ...]
) -> list[str]:
    """Termos investigativos presentes em nome + ``ufed:Body`` (:496-508).

    Args:
        name: nome do doc (comparação folded).
        props: propriedades do doc.
        terms: termos de ``search_terms`` (vazio = política "sem termo").

    Returns:
        Termos que casaram, na ordem de ``terms``; vazio sem termos.
    """
    if not terms:
        return []
    body = first_str(props, (knowledge.COMMUNICATION_BODY_FIELD,)).lower()
    search_text = f"{name.lower()} {body}"
    return [term for term in terms if term.lower() in search_text]


def _record_message(
    props: dict[str, Any],
    owners: set[str],
    chats: dict[tuple[str, bool], _ChatRecord],
    terms: tuple[str, ...],
    term_docs: dict[str, int],
) -> None:
    """Agrega uma mensagem/conversa ao registro do interlocutor (:699-717).

    Args:
        props: propriedades do doc.
        owners: identificadores do dono.
        chats: registros por ``(nome normalizado, é grupo)`` (mutado).
        terms: termos investigativos (vazio = política "sem termo").
        term_docs: contagem de docs por termo (mutada; alimenta
            ``top_terms`` sem nenhuma query nova, §8.6).

    Returns:
        Nada.
    """
    flag = first_str(props, knowledge.COMMUNICATION_GROUP_FLAG_FIELDS)
    is_group = flag.lower() == "true"
    candidate = _interlocutor_from_to(
        first_str(props, (knowledge.COMMUNICATION_TO_FIELD,)), is_group, owners
    )
    if not candidate:
        candidate = first_str(props, (knowledge.COMMUNICATION_CHAT_ID_FIELD,))
    display = _normalize_display_name(candidate, is_group, owners)
    record = chats.setdefault((display, is_group), _ChatRecord(display, is_group))
    record.message_count += 1
    chat_total = _safe_int(
        first_str(props, (knowledge.COMMUNICATION_MESSAGES_COUNT_FIELD,))
    )
    if chat_total > record.chat_total_messages:
        record.chat_total_messages = chat_total
    hits = _find_investigative_hits(
        first_str(props, (knowledge.NAME_FIELD,)), props, terms
    )
    for term in hits:
        if term not in record.terms:
            record.terms.append(term)
        term_docs[term] = term_docs.get(term, 0) + 1


def _record_interaction(
    body: str,
    owners: set[str],
    chats: dict[tuple[str, bool], _ChatRecord],
) -> None:
    """Log de interação (InteractionC): interlocutor pelo corpo (:404-425).

    Args:
        body: corpo ``ufed:Body`` do log.
        owners: identificadores do dono (autointeração ignorada, :422-425).
        chats: registros por chave (mutado).

    Returns:
        Nada. Corpo sem o marcador literal ``participent identifier:``
        (sic — datilografado no ICARO :408, preservado byte a byte) não
        produz registro.
    """
    match = _RE_INTERACTION_BODY.search(body)
    if not match:
        return
    identifier, person = match.group(1), match.group(2)
    if _GROUP_JID_SUFFIX in identifier:
        display, is_group = (person.upper() if person else _WHATSAPP_GROUP_LABEL), True
    elif person:
        display, is_group = f"{person.upper()} ({identifier})", False
    else:
        display, is_group = re.sub(r"\D", "", identifier), False
    if not display or display.lower() in owners:
        return
    record = chats.setdefault((display, is_group), _ChatRecord(display, is_group))
    record.message_count += 1


def _search_history_record(
    props: dict[str, Any], source: str
) -> dict[str, Any] | None:
    """Registro de busca de um doc de navegador (:390-398; handler :950-960).

    Args:
        props: propriedades do doc.
        source: ``ufed:Source`` minúsculo (app de origem).

    Returns:
        ``{query, timestamp, source_app, score, relevance}`` (score 40,
        BAIXA, query truncada a ``COMM_QUERY_TRUNC_CHARS``) ou ``None``
        quando ``ufed:Value`` está vazio (nenhuma busca a registrar).
    """
    value = first_str(props, (knowledge.UFED_VALUE_FIELD,)).strip()
    if not value:
        return None
    return {
        "query": value[: knowledge.COMM_QUERY_TRUNC_CHARS],
        "timestamp": first_str(
            props, (knowledge.COMMUNICATION_TIMESTAMP_FIELD,)
        )
        or knowledge.COMMUNICATION_TIMESTAMP_FALLBACK,
        "source_app": source,
        "score": knowledge.SCORE_SEARCH_HISTORY,
        "relevance": knowledge.RELEVANCE_BAIXA,
    }


def _process_documents(
    pairs: list[tuple[int, dict | None]],
    terms: tuple[str, ...],
) -> tuple[
    dict[tuple[str, bool], _ChatRecord], list[dict[str, Any]], dict[str, int]
]:
    """Roteia cada doc para busca, interação ou chat (fluxo :684-717).

    Args:
        pairs: docs varridos ``(doc_id, properties|None)``.
        terms: termos investigativos (vazio = política "sem termo").

    Returns:
        ``(chats, buscas ordenadas por timestamp desc, docs por termo)`` —
        a ordenação temporal descendente com ``or ""`` é a do ICARO (:742).
    """
    owners = _owner_identifiers(pairs)
    chats: dict[tuple[str, bool], _ChatRecord] = {}
    searches: list[dict[str, Any]] = []
    term_docs: dict[str, int] = {}
    for _, props in pairs:
        if not props:
            continue
        source = first_str(props, (knowledge.UFED_SOURCE_FIELD,)).lower()
        if not _accepts(props, source):
            continue
        if source in knowledge.COMMUNICATION_BROWSERS:
            search = _search_history_record(props, source)
            if search:
                searches.append(search)
            continue
        body = first_str(props, (knowledge.COMMUNICATION_BODY_FIELD,))
        if source == knowledge.COMMUNICATION_INTERACTION_SOURCE and body:
            _record_interaction(body, owners, chats)
            continue
        _record_message(props, owners, chats, terms, term_docs)
    searches.sort(key=lambda item: item["timestamp"] or "", reverse=True)
    return chats, searches, term_docs


# --- pontuação e visão (draft_report:865-875; estratégia :732-743) ---------------


def _score_record(record: _ChatRecord) -> dict[str, Any]:
    """Projeta o registro com score/relevância do handler ICARO (:866-869).

    Args:
        record: agregado de um interlocutor.

    Returns:
        Dicionário de saída com ``total`` interno já resolvido: com termos
        ``min(50+total, 95)``/ALTA; sem termos ``min(40+total//2, 70)`` e
        MÉDIA apenas se ``total > 50`` (comparação ESTRITA), senão BAIXA.
    """
    total = max(record.chat_total_messages, record.message_count)
    has_terms = bool(record.terms)
    if has_terms:
        score = min(
            knowledge.COMM_SCORE_TERM_BASE + total, knowledge.COMM_SCORE_TERM_CAP
        )
        relevance = knowledge.RELEVANCE_ALTA
    else:
        score = min(
            knowledge.COMM_SCORE_NOTERM_BASE
            + total // knowledge.COMM_SCORE_NOTERM_DIV,
            knowledge.COMM_SCORE_NOTERM_CAP,
        )
        relevance = (
            knowledge.RELEVANCE_MEDIA
            if total > knowledge.CHAT_RELEVANCE_MEDIA_MIN
            else knowledge.RELEVANCE_BAIXA
        )
    return {
        "interlocutor": record.interlocutor,
        "is_group": record.is_group,
        "message_count": record.message_count,
        "chat_total_messages": record.chat_total_messages,
        "terms_hits": list(record.terms),
        "score": score,
        "relevance": relevance,
    }


def _chats_view(
    chats: dict[tuple[str, bool], _ChatRecord],
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Monta a visão de chats com a política de tetos do ICARO (:732-741).

    Args:
        chats: registros por ``(nome, é grupo)`` já pontuáveis.

    Returns:
        ``(visão [:30] marcada, indivíduos pré-teto, grupos pré-teto,
        omitidos da visão)`` — grupos SEMPRE mantidos (exceto
        ``GRUPO DESCONHECIDO``, que conta como indivíduo, :735-738);
        indivíduos no top-``CHAT_INDIVIDUALS_TOP`` por interações; a lista
        final é cortada em ``CHATS_VIEW_MAX`` (corte marcado, §8.6).
    """
    scored = sorted(
        (_score_record(rec) for rec in chats.values()),
        key=lambda item: max(item["chat_total_messages"], item["message_count"]),
        reverse=True,
    )
    groups = [
        item
        for item in scored
        if item["is_group"]
        and item["interlocutor"] != knowledge.COMMUNICATION_UNKNOWN_GROUP_LABEL
    ]
    individuals = [item for item in scored if item not in groups]
    kept = individuals[: knowledge.CHAT_INDIVIDUALS_TOP] + groups
    view = kept[: knowledge.CHATS_VIEW_MAX]
    return (
        view,
        len(individuals),
        len(groups),
        len(scored) - len(view),
    )


# --- envelope e tool (DESIGN 09-B §8.6-§8.7) --------------------------------------


def _truncated_payload(
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
    chats_omitted: int,
    searches_omitted: int,
) -> dict[str, Any]:
    """Bloco ``truncated`` do envelope (§8.6): todos os cortes marcados.

    Args:
        scan: contadores da varredura (caps de query/varredura).
        budget: orçamento de GET da chamada.
        docs_omitted: docs sem props por orçamento (contrato [0]).
        chats_omitted: chats além de ``CHATS_VIEW_MAX`` (pré-corte).
        searches_omitted: buscas além de ``SEARCH_HISTORY_MAX`` (pré-corte).

    Returns:
        Dicionário ``truncated`` com cada corte explicitado.
    """
    return {
        "scan_cap_applied": scan.scan_cap_applied,
        "query_caps_applied": list(scan.query_caps),
        "chats_omitted": chats_omitted,
        "search_history_omitted": searches_omitted,
        "http_budget_exceeded": budget.exceeded,
        "http_budget_calls_at_stop": budget.used if budget.exceeded else None,
        # docs_omitted carrega a QUANTIDADE num único elemento (contrato
        # _fetch_within_budget, identity.py:1204-1206) — sum(), não len().
        "docs_omitted_by_budget": sum(docs_omitted),
    }


def _payload(
    sid: str,
    started: float,
    chats: dict[tuple[str, bool], _ChatRecord],
    searches: list[dict[str, Any]],
    term_docs: dict[str, int],
    terms: tuple[str, ...],
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
) -> dict[str, Any]:
    """Envelope final §8.6: chats + buscas + contadores M-9 + http_calls.

    Args:
        sid: source triado.
        started: ``time.monotonic()`` do início.
        chats: registros agregados (pré-teto).
        searches: buscas ordenadas (pré-teto).
        term_docs: nº de docs por termo investigativo (só com ``terms``).
        terms: termos efetivos da chamada.
        scan: contadores da varredura.
        budget: orçamento de GET.
        docs_omitted: docs sem props por orçamento (contrato [0]).

    Returns:
        Envelope da chamada; ``message`` explícita quando o eixo está vazio
        (§0.1 — None do ICARO nunca é publicado) e warning ``agregada-v1``
        sempre que houver chat (D-9).
    """
    scan.warnings.extend(_budget_warnings(budget, docs_omitted))
    chats_view, individuals_total, groups_total, chats_omitted = _chats_view(chats)
    counters = dict(
        op_envelope(started, scanned=scan.docs_searched, processed=len(chats) + len(searches))
    )
    counters.update(
        docs_searched=scan.docs_searched,
        docs_fetched=scan.docs_fetched,
        http_calls=budget.used,
        cached=False,
    )
    if budget.exceeded:
        counters["phase"] = PHASE_PARTIAL
    if chats_view:
        scan.warnings.append(knowledge.COMMUNICATION_AGG_WARNING)
    empty = not chats_view and not searches
    return {
        "source_id": sid,
        "chats": chats_view,
        "searches": searches[: knowledge.SEARCH_HISTORY_MAX],
        "chats_individuals_total": individuals_total,
        "chats_grupos_total": groups_total,
        **({"top_terms": term_docs} if terms else {}),
        "query_hits": scan.query_hits,
        "counters": counters,
        "truncated": _truncated_payload(
            scan,
            budget,
            docs_omitted,
            chats_omitted,
            max(len(searches) - knowledge.SEARCH_HISTORY_MAX, 0),
        ),
        "warnings": scan.warnings,
        **({"message": knowledge.COMMUNICATION_EMPTY_MESSAGE} if empty else {}),
    }


def build_triage_communication(
    source_id: str, search_terms: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Executa a triagem de comunicação (motor T-8).

    Args:
        source_id: source resolvido no Router (erros M-2 viram ValueError).
        search_terms: termos investigativos (§8.1); vazio = exclusivamente
            a política "sem termo" — nenhum score inventado.

    Returns:
        Envelope §8.6 da chamada corrente (agregados; nunca texto de
        mensagens nem a lista bruta de docs).
    """
    sid = (source_id or "").strip()
    if not sid:
        raise ValueError(_EMPTY_SID_MESSAGE)
    get_router().resolve(sid)
    terms = tuple(term for term in search_terms if term.strip())
    started = time.monotonic()
    scan = _Scan()
    budget = _Budget(knowledge.TOOL_BUDGETS["triage_communication"])
    queries = _communication_queries()
    ids = _collect_candidates(sid, queries, scan, budget)
    scan.docs_searched = len(ids)
    omitted: list[int] = []
    pairs = _fetch_within_budget(sid, ids, budget, omitted)
    scan.docs_fetched = sum(1 for _, props in pairs if props)
    chats, searches, term_docs = _process_documents(pairs, terms)
    return _payload(
        sid, started, chats, searches, term_docs, terms, scan, budget, omitted
    )


def triage_communication(
    source_id: str, search_terms: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Triagem de comunicação do ICARO (T-8): chats agregados + buscas.

    Args:
        source_id: id do source no servidor WebAPI do caso.
        search_terms: termos investigativos opcionais (padrão vazio); com
            termos vale ``min(50+total, 95)``/ALTA, sem termos
            ``min(40+total//2, 70)``/MÉDIA apenas com ``total > 50``.

    Returns:
        Envelope ``{source_id, chats[:30] (interlocutor, is_group,
        message_count, chat_total_messages, terms_hits, score, relevance;
        grupos sempre mantidos, indivíduos no top-20), searches[:50] (query,
        timestamp, source_app, score 40, BAIXA), chats_individuals_total,
        chats_grupos_total[, top_terms] (só com search_terms), query_hits,
        counters (+http_calls), truncated, warnings[, message]}`` — o corpo
        das mensagens NUNCA é publicado; agregação "agregada-v1" (sem mescla
        por chat-id, D-9, declarado em warning).

    Raises:
        ToolError: source inexistente/servidor offline (M-2) ou erro
            inesperado traduzido; estouro de orçamento NUNCA raising — vira
            ``truncated.http_budget_exceeded`` + warning.
    """
    try:
        return build_triage_communication(source_id, search_terms)
    except ValueError:
        raise
    except Exception as exc:  # traduz erro inesperado (padrão da família)
        raise ToolError(f"triage_communication falhou: {exc}") from exc


def register(mcp: FastMCP) -> None:
    """Registra a tool de triagem de comunicação no servidor.

    Args:
        mcp: servidor FastMCP ativo.

    Returns:
        Nada.
    """
    mcp.add_tool(triage_communication)
