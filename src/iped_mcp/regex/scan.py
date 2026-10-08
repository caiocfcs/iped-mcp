"""Núcleo de escaneamento de atributos `Regex:*` (busca + fetch paralelo + cache)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from iped_mcp.fetch import cache_get as _cache_get
from iped_mcp.fetch import cache_set as _cache_set
from iped_mcp.fetch import fetch_docs_parallel, search_ids as _search_ids
from iped_mcp.regex.queries import KNOWN_TYPES, build_regex_query

_REGEX_HINT = "Use regex_list_types para conferir os tipos válidos."

# Busca match-all (`*`) demora (~47 s no source de teste); timeout padrão (10 s) não basta.
_MATCH_ALL_TIMEOUT = 120.0


def search_ids(query: str, source_id: str, timeout: float | None = None) -> list[int]:
    """Busca no IPED e devolve a lista achatada de doc ids."""
    return _search_ids(query, source_id, _REGEX_HINT, timeout=timeout)


def _first(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


@dataclass
class DocMatch:
    doc_id: int
    name: str | None
    md5: str | None
    values: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    total_docs: int
    matches: list[DocMatch] = field(default_factory=list)


def scan_attribute(source_id: str, attribute: str) -> ScanResult:
    """Escaneia todos os docs onde o detector `attribute` casou.

    Coleta os valores de `Regex:{attribute}` de cada doc (um doc pode ter
    vários). Cache em memória por (source, attribute) com TTL de 5 min.
    """
    key = ("scan", source_id, attribute)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    ids = search_ids(build_regex_query(attribute), source_id)
    matches: list[DocMatch] = []
    for doc_id, props in fetch_docs_parallel(source_id, ids):
        if not props:
            continue
        raw = props.get(f"Regex:{attribute}")
        values = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
        values = [str(v) for v in values if v is not None]
        if not values:
            continue
        md5 = _first(props.get("md5"))
        matches.append(
            DocMatch(
                doc_id=doc_id,
                name=_first(props.get("name")),
                md5=str(md5).lower() if md5 is not None else None,
                values=values,
            )
        )
    result = ScanResult(total_docs=len(ids), matches=matches)
    _cache_set(key, result)
    return result


def _stratified_sample(ids: list[int], n: int) -> list[int]:
    """Escolhe n ids espaçados ao longo da lista (não só os primeiros)."""
    if len(ids) <= n:
        return list(ids)
    step = len(ids) / n
    return [ids[int(i * step)] for i in range(n)]


def discover_types(source_id: str, sample_size: int) -> dict[str, Any]:
    """Descobre os tipos `Regex:*` presentes no source.

    Tipos conhecidos (KNOWN_TYPES) recebem contagem exata de docs via busca
    (sem ler docs). Tipos fora da lista são descobertos lendo as chaves
    `Regex:` de `sample_size` docs amostrados estratificadamente de uma busca
    match-all (cara; cacheada por source).
    """
    key = ("types", source_id)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    types: dict[str, dict[str, Any]] = {}

    def _known(name: str) -> tuple[str, int]:
        return name, len(search_ids(build_regex_query(name), source_id))

    # As 9 buscas de tipos conhecidos + a match-all rodam em paralelo
    # (sequenciais custavam ~10-15 s a mais; a match-all domina o tempo).
    with ThreadPoolExecutor(max_workers=len(KNOWN_TYPES) + 1) as pool:
        known_futures = [pool.submit(_known, name) for name in KNOWN_TYPES]
        all_future = pool.submit(search_ids, "*", source_id, _MATCH_ALL_TIMEOUT)
        for fut in known_futures:
            name, count = fut.result()
            if count:
                types[name] = {"docs": count, "how": "exact"}
        all_ids = all_future.result()
    sampled_ids = _stratified_sample(all_ids, sample_size)
    for _doc_id, props in fetch_docs_parallel(source_id, sampled_ids):
        if not props:
            continue
        for prop_key in props:
            if prop_key.startswith("Regex:"):
                name = prop_key.split(":", 1)[1]
                if name not in types:
                    types[name] = {"docs": 0, "how": "sample"}
                types[name]["docs"] += 1
    result = {
        "sampled": len(sampled_ids),
        "types": dict(sorted(types.items(), key=lambda kv: -kv[1]["docs"])),
    }
    _cache_set(key, result)
    return result
