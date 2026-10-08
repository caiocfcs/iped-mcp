"""Coleta cacheada das mensagens WA do source (método validado nas Fases 2–3).

Coletar uma vez e analisar localmente: uma busca canônica + fetch paralelo de
todos os docs de mensagem alimenta overview/conversation/export/text_search.
Cache em memória por source com TTL de 5 min (padrão de regex/scan.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from iped_mcp.fetch import cache_get, cache_set, fetch_docs_parallel, search_ids
from iped_mcp.whatsapp.queries import (
    build_messages_query,
    classify_name,
    extract_number,
)

_WA_HINT = "Use whatsapp_reference para conferir os campos e queries canônicas."

_LINKED_HASH_RE = re.compile(r"sha-256:([0-9a-fA-F]+)")


def _first(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


@dataclass
class MessageRecord:
    doc_id: int
    name: str
    key: str | None
    kind: str
    date: str | None
    from_number: str | None
    to_number: str | None
    linked_hashes: list[str] = field(default_factory=list)


def _linked_hashes(props: dict[str, Any]) -> list[str]:
    raw = props.get("linkedItems")
    items = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
    hashes: list[str] = []
    for item in items:
        m = _LINKED_HASH_RE.search(str(item))
        if m:
            hashes.append(m.group(1).lower())
    return hashes


def collect_messages(
    source_id: str,
) -> tuple[list[MessageRecord], dict[str, set[str]], int]:
    """Coleta todas as mensagens WA do source → (records, sha_map, dropped).

    `records`: um `MessageRecord` por doc de mensagem (categoria Instant
    Messages), com chave/tipo extraídos do `name`, data, números de From/To e
    hashes de `linkedItems`.
    `sha_map`: sha-256 (lowercase) → conjunto de chaves de conversas privadas
    que referenciam a mídia (link unidirecional mensagem→mídia, F1.17; só
    conversas privadas). Mídia encaminhada aparece em >1 conversa: o hit conta
    para todas (validado: `pix` → 62 conversas em anexo).
    `dropped`: nº de docs cuja busca retornou id mas o fetch falhou (props
    vazios) — sinaliza perda de dados parcial na coleta.
    """
    key = ("wa_collect", source_id)
    cached = cache_get(key)
    if cached is not None:
        return cached
    ids = search_ids(build_messages_query(), source_id, _WA_HINT)
    records: list[MessageRecord] = []
    dropped = 0
    for doc_id, props in fetch_docs_parallel(source_id, ids):
        if not props:
            dropped += 1
            continue
        name = str(_first(props.get("name")) or "")
        kind, conv_key = classify_name(name)
        date = _first(props.get("Communication:Date"))
        records.append(
            MessageRecord(
                doc_id=doc_id,
                name=name,
                key=conv_key,
                kind=kind,
                date=str(date) if date is not None else None,
                from_number=extract_number(str(_first(props.get("Communication:From")) or "")),
                to_number=extract_number(str(_first(props.get("Communication:To")) or "")),
                linked_hashes=_linked_hashes(props),
            )
        )
    sha_map: dict[str, set[str]] = {}
    for rec in records:
        if rec.kind == "chat" and rec.key:
            for h in rec.linked_hashes:
                sha_map.setdefault(h, set()).add(rec.key)
    result = (records, sha_map, dropped)
    cache_set(key, result)
    return result
