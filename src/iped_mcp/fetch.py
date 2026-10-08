"""Busca + fetch paralelo de docs e cache em memória (compartilhado entre domínios)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from iped_mcp.config import WORKERS
from iped_mcp.diagnose import diagnose_500
from iped_mcp.router import get_router

_CACHE_TTL = 300.0
_cache: dict[tuple, tuple[float, Any]] = {}

# --- cache de texto extraído (evita re-baixar o /text inteiro na paginação) ---
# doc_text paginado re-baixava o texto completo (até 10 MB) a cada chunk;
# whatsapp_text_search re-baixava todos os PDFs a cada termo. TTL + teto total
# de chars (evacua o mais antigo) limitam a RAM.
_TEXT_CACHE_TTL = 300.0
_TEXT_CACHE_MAX_CHARS = 256 * 1024 * 1024  # 256 MB
_text_cache: dict[tuple[str, int], tuple[float, tuple[str, int, bool]]] = {}
_text_cache_lock = threading.Lock()


def text_cache_get(source_id: str, doc_id: int) -> tuple[str, int, bool] | None:
    """(text, total_chars, capped) do cache de texto; None se ausente/expirado."""
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


def text_cache_set(source_id: str, doc_id: int, value: tuple[str, int, bool]) -> None:
    """Armazena (text, total_chars, capped); evacua o mais antigo acima do teto."""
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
    attempts: int = 3,
) -> list[int]:
    """Busca no IPED e devolve a lista achatada de doc ids.

    Falhas transitórias (conexão, timeout) são retried antes de desistir;
    HTTP 500 (sintaxe da query) falha na hora com o diagnóstico.
    """
    client = get_router()
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            results = client.search(query, source_id=source_id, timeout=timeout)
            return [doc_id for group in results for doc_id in group.get("ids", [])]
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 500:
                raise ValueError(diagnose_500(query, hint)) from exc
            last_exc = exc
        except requests.RequestException as exc:
            last_exc = exc
        except ValueError as exc:
            # o router converte ConnectionError/Timeout em ValueError centrado
            # em source; com paralelismo alto essas falhas transitórias são
            # mais frequentes — trata como retry.
            last_exc = exc
        if i < attempts - 1:
            time.sleep(1.0 * (2**i))
    raise ValueError(
        f"Source '{source_id}' indisponível: o servidor correspondente "
        "não responde."
    ) from last_exc


def _retry(fn, attempts: int = 3):
    """Executa fn() com retry em falhas transitórias (5xx, conexão, timeout).

    Devolve o resultado de fn(); None em falha permanente (4xx) ou esgotado.
    """
    for i in range(attempts):
        try:
            return fn()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500:
                return None
        except (requests.ConnectionError, requests.Timeout):
            pass
        except ValueError:
            # o router converte ConnectionError/Timeout em ValueError centrado
            # em source; com paralelismo alto essas falhas transitórias são
            # mais frequentes — trata como retry (não derruba o lote).
            pass
        except requests.RequestException:
            return None
        if i < attempts - 1:
            time.sleep(0.5 * (2**i))
    return None


def get_document_props(source_id: str, doc_id: int, attempts: int = 3) -> dict | None:
    """GET /docs/{id} → properties (com retry); None em doc inexistente/falha."""
    client = get_router()
    doc = _retry(lambda: client.get_document(source_id, doc_id), attempts)
    return doc.get("properties", {}) if doc is not None else None


def get_document_text_cached(
    source_id: str, doc_id: int, attempts: int = 3
) -> tuple[str, int, bool] | None:
    """GET /docs/{id}/text → (text, total_chars, capped) com cache + retry.

    None em falha permanente (4xx) ou retry esgotado.
    """
    cached = text_cache_get(source_id, doc_id)
    if cached is not None:
        return cached
    client = get_router()
    result = _retry(lambda: client.get_document_text(source_id, doc_id), attempts)
    if result is None:
        return None
    text_cache_set(source_id, doc_id, result)
    return result


def get_document_text(source_id: str, doc_id: int, attempts: int = 3) -> str | None:
    """GET /docs/{id}/text → texto (cache + retry); None em falha/esgotado."""
    result = get_document_text_cached(source_id, doc_id, attempts)
    if result is None:
        return None
    return result[0]


def fetch_docs_parallel(
    source_id: str, ids: list[int], workers: int = WORKERS
) -> list[tuple[int, dict | None]]:
    """Busca documentos em paralelo; devolve (doc_id, properties) na ordem dos ids.

    Doc que falhar (ex.: removido) vira (doc_id, None) e não derruba o lote;
    falhas transitórias (5xx, conexão, timeout) são retried antes de desistir.
    """

    def _fetch(doc_id: int) -> tuple[int, dict | None]:
        return doc_id, get_document_props(source_id, doc_id)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_fetch, ids))
