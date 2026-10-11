"""Tool ``triage_telephony`` — eixo telefonia (T-1, DESIGN 09 §2).

Porta o núcleo determinístico do eixo "Telefonia" do ICARO: varre as
categorias canônicas (:mod:`iped_mcp.triage.knowledge`), agrega chamadas e
contatos de agenda por número (semântica de ``_normalize_entities``,
iped-icaro/strategies/telephony_strategy.py:50-135) e classifica por peso
(draft_report_iped_manager.py:914-929). Nenhum LLM, nenhum arquivo do caso:
somente GET /search e GET /docs/{id} do client.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP

from iped_mcp import fetch
from iped_mcp.progress import op_envelope
from iped_mcp.router import get_router
from iped_mcp.triage import knowledge

_KIND_CALL = "call"
_KIND_CONTACT = "contact"
_SEARCH_HINT = "triage_telephony (busca de categoria de telefonia)"
_EMPTY_SID_MESSAGE = "'source_id' é obrigatório (veja iped_list_sources)."


@dataclass
class _ContactAgg:
    """Acúmulo por número durante a varredura (espelho TelephonyRecord).

    Attributes:
        phone: número-chave do agregado.
        contact_name: último nome visto (chamada) ou da agenda.
        total_calls: chamadas registradas para o número.
        calls_out: chamadas outgoing.
        calls_in: chamadas incoming.
        calls_missed: chamadas perdidas.
        total_duration_seconds: soma tolerante de ``ufed:Duration``.
        sample_doc_ids: até ``SAMPLE_DOC_IDS_MAX`` ids p/ iped_doc_info.
    """

    phone: str
    contact_name: str = knowledge.DEFAULT_CONTACT_NAME
    total_calls: int = 0
    calls_out: int = 0
    calls_in: int = 0
    calls_missed: int = 0
    total_duration_seconds: int = 0
    sample_doc_ids: list[int] = field(default_factory=list)


@dataclass
class _Scan:
    """Contadores e avisos da varredura de um source (envelope §2.5)."""

    query_hits: dict[str, int] = field(default_factory=dict)
    query_caps: list[str] = field(default_factory=list)
    total_cap_applied: bool = False
    docs_searched: int = 0
    docs_fetched: int = 0
    call_docs: int = 0
    contact_docs: int = 0
    categories_seen: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def _first_str(props: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Primeiro elemento (como string) da primeira propriedade não vazia.

    Args:
        props: propriedades do doc no envelope do jar (valores em lista).
        keys: chaves na ordem de fallback (mesma semântica ``a or b`` do
            ICARO, telephony_strategy.py:59/:77-78/:118).

    Returns:
        ``str`` do primeiro valor não vazio; ``""`` quando nada casa.
    """
    for key in keys:
        values = props.get(key) or []
        if values and str(values[0]).strip():
            return str(values[0])
    return ""


def _parse_headto(comm_to: str) -> tuple[str, str] | None:
    """Extrai ``(nome, telefone)`` do formato "Nome (+55...)".

    Args:
        comm_to: valor cru de ``Communication:To``/``From``.

    Returns:
        Tupla ``(nome, telefone)`` (nome pode ser vazio); ``None`` quando
        nem a regex nem o fallback só-dígitos (:63-74) produzem número.
    """
    match = re.search(knowledge.HEADTO_RE, comm_to)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    digits = re.sub(r"\D", "", comm_to)
    if len(digits) >= knowledge.MIN_CALL_PHONE_DIGITS:
        return "", digits
    return None


def _parse_duration(value: str) -> int:
    """Converte ``ufed:Duration`` (HH:MM:SS | MM:SS | segundos) em segundos.

    Args:
        value: valor cru da propriedade de duração.

    Returns:
        Segundos inteiros; 0 em valor vazio/inválido (comportamento B16 do
        ICARO, telephony_strategy.py:103-110).
    """
    if not value or value == "0":
        return 0
    parts = value.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(value)
    except ValueError:
        return 0


def _call_identity(props: dict[str, Any]) -> tuple[str, str]:
    """``(telefone, nome)`` de uma chamada (telephony_strategy.py:59-80).

    Args:
        props: propriedades do doc de chamada.

    Returns:
        Telefone e nome extraídos; ``("", "")`` quando não há número.
    """
    comm_to = _first_str(props, knowledge.CALL_TO_FIELDS)
    if comm_to:
        parsed = _parse_headto(comm_to)
        if parsed is None:
            return "", ""
        return parsed[1], parsed[0]
    raw = _first_str(props, knowledge.PHONE_FALLBACK_FIELDS)
    return raw.strip().strip(knowledge.QUOTE_STRIP_CHARS), ""


def _apply_call(
    contacts: dict[str, _ContactAgg], doc_id: int, props: dict[str, Any]
) -> bool:
    """Registra uma chamada no agregado do número (:57-110).

    Args:
        contacts: agregados por número (mutado).
        doc_id: id do doc processado (amostra do contato).
        props: propriedades do doc de chamada.

    Returns:
        True quando o doc produziu registro (número aproveitável).
    """
    phone, name = _call_identity(props)
    if not phone or len(phone) < knowledge.MIN_PHONE_LEN:
        return False
    record = contacts.setdefault(phone, _ContactAgg(phone=phone))
    record.contact_name = name.upper() if name else knowledge.DEFAULT_CONTACT_NAME
    record.total_calls += 1
    if len(record.sample_doc_ids) < knowledge.SAMPLE_DOC_IDS_MAX:
        record.sample_doc_ids.append(doc_id)
    direction = _first_str(props, (knowledge.DIRECTION_FIELD,)).lower()
    for marker, attr in knowledge.DIRECTION_BUCKETS.items():
        if marker in direction:
            setattr(record, attr, getattr(record, attr) + 1)
            break
    record.total_duration_seconds += _parse_duration(
        _first_str(props, (knowledge.DURATION_FIELD,))
    )
    return True


def _apply_contact(
    contacts: dict[str, _ContactAgg], doc_id: int, props: dict[str, Any]
) -> bool:
    """Contato de agenda: cria registro zero-chamada ou RENOMEIA número
    já visto por chamada (telephony_strategy.py:115-131).

    Args:
        contacts: agregados por número (mutado).
        doc_id: id do doc processado (amostra do contato).
        props: propriedades do doc de contato.

    Returns:
        True quando o doc produziu registro (nome + número válidos).
    """
    name = _first_str(props, knowledge.CONTACT_NAME_FIELDS).strip()
    clean = re.sub(r"\D", "", _first_str(props, knowledge.CONTACT_PHONE_FIELDS))
    if not name or not clean or len(clean) < knowledge.MIN_CONTACT_PHONE_DIGITS:
        return False
    if clean in contacts:
        contacts[clean].contact_name = name.upper()
    else:
        contacts[clean] = _ContactAgg(phone=clean, contact_name=name.upper())
    record = contacts[clean]
    if len(record.sample_doc_ids) < knowledge.SAMPLE_DOC_IDS_MAX:
        record.sample_doc_ids.append(doc_id)
    return True


def _classify(props: dict[str, Any]) -> str | None:
    """Categoria do doc (substring case-insensitive, :30-31) por ramificação.

    Args:
        props: propriedades do doc.

    Returns:
        ``"call"``, ``"contact"`` ou ``None`` (categoria fora do eixo).
    """
    category = _first_str(props, (knowledge.CATEGORY_FIELD,)).lower()
    if not any(cat in category for cat in knowledge.TELEPHONY_CATEGORIES):
        return None
    if any(marker in category for marker in knowledge.CALL_BRANCH_MARKERS):
        return _KIND_CALL
    if any(marker in category for marker in knowledge.CONTACT_BRANCH_MARKERS):
        return _KIND_CONTACT
    return None


def _collect_candidate_ids(sid: str, scan: _Scan) -> list[int]:
    """Busca as categorias canônicas com dedup global e tetos (§2.3.2).

    Args:
        sid: source já resolvido (M-2).
        scan: contadores da varredura (mutado; query falha vira warning e
            hits -1, espelho base_strategy.py:122-123).

    Returns:
        ids únicos na ordem de descoberta, até ``TOTAL_CANDIDATE_CAP``.
    """
    seen: dict[int, None] = {}
    for category in knowledge.TELEPHONY_CATEGORIES:
        query = knowledge.TELEPHONY_QUERY_TEMPLATE.format(category=category)
        try:
            ids = fetch.search_ids(query, sid, hint=_SEARCH_HINT)
        except ValueError as exc:
            scan.warnings.append(f"query {query} falhou: {exc}")
            scan.query_hits[query] = -1
            continue
        scan.query_hits[query] = len(ids)
        if len(ids) > knowledge.CATEGORY_QUERY_CAP:
            scan.query_caps.append(query)
            ids = ids[: knowledge.CATEGORY_QUERY_CAP]
        for doc_id in ids:
            if len(seen) >= knowledge.TOTAL_CANDIDATE_CAP:
                scan.total_cap_applied = True
                break
            seen[doc_id] = None
    return list(seen)


def _aggregate(pairs: list[tuple[int, dict | None]], scan: _Scan) -> dict[str, _ContactAgg]:
    """Classifica e agrega os docs fetcheados (passos §2.3.4-5; doc ``None``
    — sumiu no meio da varredura — só não entra em ``docs_fetched``).

    Args:
        pairs: ``(doc_id, properties|None)`` na ordem dos ids.
        scan: contadores da varredura (mutado).

    Returns:
        Agregados por número, na ordem de primeira aparição.
    """
    contacts: dict[str, _ContactAgg] = {}
    for doc_id, props in pairs:
        if props is None:
            continue
        kind = _classify(props)
        if kind is None:
            continue
        scan.categories_seen.add(
            _first_str(props, (knowledge.CATEGORY_FIELD,)).lower()
        )
        if kind == _KIND_CALL:
            if _apply_call(contacts, doc_id, props):
                scan.call_docs += 1
        elif _apply_contact(contacts, doc_id, props):
            scan.contact_docs += 1
    return contacts


def _contact_row(record: _ContactAgg) -> dict[str, Any]:
    """Linha de saída do contato (shape §2.4; score/justificação :914-925).

    Args:
        record: agregado do número.

    Returns:
        Registro compacto com phone/contact_name/contadores/score/relevance/
        justification/sample_doc_ids.
    """
    if record.total_calls > knowledge.RELEVANCE_ALTA_MIN_CALLS:
        relevance = knowledge.RELEVANCE_ALTA
    elif record.total_calls > knowledge.RELEVANCE_MEDIA_MIN_CALLS:
        relevance = knowledge.RELEVANCE_MEDIA
    else:
        relevance = knowledge.RELEVANCE_BAIXA
    return {
        "phone": record.phone,
        "contact_name": record.contact_name,
        "total_calls": record.total_calls,
        "calls_out": record.calls_out,
        "calls_in": record.calls_in,
        "calls_missed": record.calls_missed,
        "total_duration_seconds": record.total_duration_seconds,
        "score": min(
            knowledge.RELEVANCE_BASE + record.total_calls, knowledge.RELEVANCE_CAP
        ),
        "relevance": relevance,
        "justification": knowledge.JUSTIFICATION_TEMPLATE.format(
            calls=record.total_calls,
            out=record.calls_out,
            inn=record.calls_in,
            missed=record.calls_missed,
        ),
        "sample_doc_ids": list(record.sample_doc_ids),
    }


def _rank_contacts(contacts: dict[str, _ContactAgg]) -> list[dict[str, Any]]:
    """Ordena (relevance ALTA<MÉDIA<BAIXA, depois -score) — :483-484.

    Args:
        contacts: agregados por número.

    Returns:
        Linhas de saída ordenadas (todas; o teto TOP_CONTACTS é aplicado no
        envelope com marcação de truncamento).
    """
    rows = [_contact_row(record) for record in contacts.values()]
    rows.sort(key=lambda row: (knowledge.RELEVANCE_ORDER[row["relevance"]], -row["score"]))
    return rows


def _zero_hit_warnings(scan: _Scan) -> list[str]:
    """Avisos §2.3.4/§6 (TO_VERIFY-1): query exata sem hits mas a substring
    da categoria apareceu nos docs trazidos por outras queries (categoria
    decorada no jar real).

    Args:
        scan: contadores da varredura.

    Returns:
        Avisos (vazio quando nenhuma categoria decorada é suspeita).
    """
    out: list[str] = []
    for category in knowledge.TELEPHONY_CATEGORIES:
        query = knowledge.TELEPHONY_QUERY_TEMPLATE.format(category=category)
        if scan.query_hits.get(query) == 0 and any(
            category in seen for seen in scan.categories_seen
        ):
            out.append(
                f"categoria '{category}': query exata sem hits, mas a substring "
                "casou em docs de outras queries (categoria decorada no jar?)"
            )
    return out


def _payload(
    sid: str, started: float, rows: list[dict[str, Any]], scan: _Scan
) -> dict[str, Any]:
    """Monta o envelope final (§2.5): contadores M-9 + truncamento marcado.

    Args:
        sid: source triado.
        started: ``time.monotonic()`` do início da operação.
        rows: contatos já ordenados.
        scan: contadores da varredura.

    Returns:
        Envelope com ``contacts`` limitado a ``TOP_CONTACTS`` (omissão
        sempre marcada em ``truncated``); ``message`` quando vazio.
    """
    scan.warnings.extend(_zero_hit_warnings(scan))
    return {
        "source_id": sid,
        "contacts": rows[: knowledge.TOP_CONTACTS],
        "query_hits": dict(scan.query_hits),
        "counters": dict(
            op_envelope(
                started,
                scanned=scan.docs_searched,
                processed=scan.call_docs + scan.contact_docs,
            ),
            docs_searched=scan.docs_searched,
            docs_fetched=scan.docs_fetched,
            call_docs=scan.call_docs,
            contact_docs=scan.contact_docs,
            contacts_total=len(rows),
            cached=False,
        ),
        "truncated": {
            "total_cap_applied": scan.total_cap_applied,
            "query_caps_applied": list(scan.query_caps),
            "contacts_truncated": len(rows) > knowledge.TOP_CONTACTS,
            "contacts_omitted": max(len(rows) - knowledge.TOP_CONTACTS, 0),
        },
        "warnings": list(scan.warnings),
        **(
            {"message": knowledge.TELEPHONY_EMPTY_MESSAGE}
            if not rows
            else {}
        ),
    }


def build_triage_telephony(source_id: str) -> dict[str, Any]:
    """Núcleo da triagem de telefonia (lógica testável sem MCP).

    Args:
        source_id: source a triar (obrigatório).

    Returns:
        Envelope de ``_payload`` (shape DESIGN 09 §2.5).

    Raises:
        ValueError: source_id vazio, inexistente ou endpoint offline
            (textos estáveis da política M-2, via ``Router.resolve``).
    """
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError(_EMPTY_SID_MESSAGE)
    get_router().resolve(sid)  # M-2 antes de qualquer HTTP de busca (§2.3.1)
    started = time.monotonic()
    scan = _Scan()
    ids = _collect_candidate_ids(sid, scan)
    pairs = fetch.fetch_docs_parallel(sid, ids)
    scan.docs_searched = len(ids)
    scan.docs_fetched = sum(1 for _, props in pairs if props is not None)
    rows = _rank_contacts(_aggregate(pairs, scan))
    return _payload(sid, started, rows, scan)


def triage_telephony(source_id: str) -> dict[str, Any]:
    """Triagem do eixo telefonia: top contatos por peso de chamadas.

    Varre as categorias canônicas de telefonia (``chamadas telefônicas``,
    ``calls``, ``call logs``, ``contatos``, ``contacts``), agrega chamadas
    e contatos de agenda por número e devolve os 20 mais relevantes com
    score ``min(50 + total_calls, 90)`` e relevância ``ALTA`` (>30
    chamadas), ``MÉDIA`` (>10) ou ``BAIXA`` — heurística do fluxo ICARO,
    sem LLM. A resposta traz somente registros compactos + contadores:
    nenhum id de documento fora de ``sample_doc_ids`` (até 3 por contato)
    e nenhuma propriedade bruta. Contatos de agenda sem chamadas vêm com
    ``total_calls=0``/``score=50``/``BAIXA``.

    Args:
        source_id: source a triar (veja iped_list_sources).

    Returns:
        {source_id, contacts: [{phone, contact_name, total_calls,
        calls_out, calls_in, calls_missed, total_duration_seconds, score,
        relevance, justification, sample_doc_ids}], query_hits (por query;
        -1 = query que falhou), counters (phase/scanned/processed/
        elapsed_s/docs_searched/docs_fetched/call_docs/contact_docs/
        contacts_total/cached), truncated (tetos SEMPRE marcados),
        warnings}; ``message`` quando o eixo está vazio (contacts=[],
        nunca exceção).

    Raises:
        ValueError: source_id vazio, inexistente ou servidor indisponível
            (textos estáveis da política M-2).
    """
    return build_triage_telephony(source_id)


def register(mcp: FastMCP) -> None:
    """Registra a tool de triagem de telefonia (T-1) no servidor.

    Args:
        mcp: servidor FastMCP ativo.
    """
    mcp.add_tool(triage_telephony)
