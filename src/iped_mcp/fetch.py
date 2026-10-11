"""Busca + fetch paralelo de docs e cache em memória (compartilhado entre domínios).

Resiliência (M-3): o retry de falha transitória é política do CLIENT
(`iped_mcp.resilience`, aplicada a TODOS os chamados); este módulo faz uma
única chamada por operação e trata apenas a falha PERMANENTE — doc
inexistente vira `None` (não derruba lote) e 500 de query no /search vira
diagnóstico imediato (AC4: sem esperar tentativas).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from iped_mcp.client import TextResult
from iped_mcp.config import WORKERS
from iped_mcp.diagnose import diagnose_500
from iped_mcp.errors import IpedError, ServerError
from iped_mcp.router import get_router

_CACHE_TTL = 300.0
_cache: dict[tuple, tuple[float, Any]] = {}

# --- cache de texto extraído (evita re-baixar o /text inteiro na paginação) ---
# doc_text paginado re-baixava o texto completo (até 10 MB) a cada chunk;
# whatsapp_text_search re-baixava todos os PDFs a cada termo. TTL + teto total
# de chars (evacua o mais antigo) limitam a RAM.
_TEXT_CACHE_TTL = 300.0
_TEXT_CACHE_MAX_CHARS = 256 * 1024 * 1024  # 256 MB
_text_cache: dict[tuple[str, int], tuple[float, TextResult]] = {}
_text_cache_lock = threading.Lock()

# Falha esperada das rotas roteadas: `IpedError` (taxonomia do client, já
# com retry esgotado), `ValueError` (tradução centrada em source do router)
# e `requests.RequestException` (defensivo — os erros da taxonomia herdam
# dela). Todas viram o contrato de "falhou" de cada função abaixo.
_EXPECTED_FAILURES = (IpedError, ValueError, requests.RequestException)


def text_cache_get(source_id: str, doc_id: int) -> TextResult | None:
    """`TextResult` do cache de texto; None se ausente/expirado."""
    key = (source_id, doc_id)
    entry = _text_cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts >= _TEXT_CACHE_TTL:
        with _text_cache_lock:
            _text_cache.pop(key, None)
        return None
    return value


def text_cache_set(source_id: str, doc_id: int, value: TextResult) -> None:
    """Armazena o `TextResult`; evacua o mais antigo acima do teto."""
    key = (source_id, doc_id)
    with _text_cache_lock:
        _text_cache[key] = (time.monotonic(), value)
        total = sum(len(v[1][0]) for v in _text_cache.values())
        while total > _TEXT_CACHE_MAX_CHARS and len(_text_cache) > 1:
            oldest = min(_text_cache, key=lambda k: _text_cache[k][0])
            del _text_cache[oldest]
            total = sum(len(v[1][0]) for v in _text_cache.values())


def cache_get(key: tuple) -> Any:
    entry = _cache.get(key)
    if entry is not None and time.monotonic() - entry[0] < _CACHE_TTL:
        return entry[1]
    _cache.pop(key, None)
    return None


def cache_set(key: tuple, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


def search_ids(
    query: str,
    source_id: str,
    hint: str,
    timeout: float | None = None,
) -> list[int]:
    """Busca no IPED e devolve a lista achatada de doc ids.

    Uma única chamada: o retry de falha transitória (conexão/timeout) é do
    client (M-3) e chega aqui já traduzido pelo router em ValueError com
    texto estável ("Source 'x' indisponível"). HTTP 500 no /search = query
    rejeitada (determinística): o client não re-tenta 5xx de busca (M-3
    decide por classe) e o erro chega aqui como ValueError padrão M-2 — o
    passe-through tipado do router foi revogado no M-14 (AC4 preservado:
    o pino de não-retry do M-3 continua verde).

    Args:
        query: query Lucene.
        source_id: source roteada.
        hint: frase final do diagnóstico de 500 (contexto da tool); vale
            apenas no ramo defensivo `ServerError` abaixo.
        timeout: timeout desta chamada.

    Returns:
        Doc ids achatados (ordem dos grupos do servidor).

    Raises:
        ValueError: query rejeitada (500) ou source indisponível/inexistente.
    """
    client = get_router()
    try:
        results = client.search(query, source_id=source_id, timeout=timeout)
    except ServerError as exc:
        # Defensivo (M-14): o router não volta mais a vaziar a classe no
        # fluxo roteado; o ramo preserva o diagnóstico de query com hint
        # para um IPEDClient cru usado diretamente.
        raise ValueError(diagnose_500(query, hint)) from exc
    except IpedError as exc:
        # Defensivo: no fluxo roteado o router já converteu em ValueError.
        raise ValueError(str(exc)) from exc
    return [doc_id for group in results for doc_id in group.get("ids", [])]


def get_document_props(source_id: str, doc_id: int) -> dict | None:
    """GET /docs/{id} → properties; None em doc inexistente/falha permanente.

    O retry de falha transitória é do client (M-3); a falha esperada (doc
    removido, 500 persistente) vira None e não derruba o lote.
    """
    client = get_router()
    try:
        doc = client.get_document(source_id, doc_id)
    except _EXPECTED_FAILURES:
        return None
    return doc.get("properties", {}) if doc is not None else None


def get_document_text_cached(
    source_id: str, doc_id: int
) -> TextResult | None:
    """GET /docs/{id}/text → `TextResult` (5 campos, M-8) com cache.

    None em falha permanente (doc inexistente/esgotado — retry é do client).
    """
    cached = text_cache_get(source_id, doc_id)
    if cached is not None:
        return cached
    client = get_router()
    try:
        result = client.get_document_text(source_id, doc_id)
    except _EXPECTED_FAILURES:
        return None
    text_cache_set(source_id, doc_id, result)
    return result


def get_document_text(source_id: str, doc_id: int) -> str | None:
    """GET /docs/{id}/text → texto (com cache); None em falha/esgotado."""
    result = get_document_text_cached(source_id, doc_id)
    if result is None:
        return None
    return result[0]


def fetch_docs_parallel(
    source_id: str, ids: list[int], workers: int = WORKERS
) -> list[tuple[int, dict | None]]:
    """Busca documentos em paralelo; devolve (doc_id, properties) na ordem dos ids.

    Doc que falhar (ex.: removido) vira (doc_id, None) e não derruba o lote;
    falhas transitórias já foram re-tentadas pelo client (M-3).
    """

    def _fetch(doc_id: int) -> tuple[int, dict | None]:
        return doc_id, get_document_props(source_id, doc_id)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_fetch, ids))
