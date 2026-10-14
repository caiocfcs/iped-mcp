"""Censo de caso do lado do cliente: contagem por categoria (DESIGN 06 / M-6).

A tool `case_summary` responde à primeira pergunta de qualquer triagem —
"o que este caso contém?" — sem despejar ids no contexto do agente. Os
jars em execução NÃO expõem /case-summary server-side (F-5 ausente, 404
— 2d 03), então o censo é montado no cliente no padrão já validado no
ICARO (iped_category_census.py:133-243):

1. GET /categories → taxonomia real do índice;
2. para cada categoria, GET /search q='category:"<cat>"' com
   concorrência limitada a CENSUS_WORKERS;
3. contagem = nº de ids retornados (o IPED não pagina — WI-1);
4. categoria que falha vira count=-1 + warning e NUNCA derruba o censo
   (padrão não-destrutivo do ICARO, :141-164);
5. cache em memória por fingerprint (source + hash do catálogo de
   categorias) com TTL CENSUS_CACHE_TTL_SECONDS — o /categories (1
   request barato) é reconsultado a cada chamada para detectar mudança de
   taxonomia; as N buscas caras são reutilizadas enquanto o fingerprint
   bater (AC5).

Guarda de contexto: apenas contagens são emitidas — listas completas de
ids nunca entram no retorno nem nos logs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import requests
from fastmcp import FastMCP

from iped_mcp.diagnose import guarded_call
from iped_mcp.router import get_router

logger = logging.getLogger("iped_mcp.core.census")

# Espelha _COUNT_WORKERS do ICARO (iped_category_census.py:31): as contagens
# não leem propriedades, mas oneram o MESMO jar do pipeline ICARO.
CENSUS_WORKERS = 4
# TTL do cache em memória; mesma ordem dos caches de texto/scans de fetch.py.
CENSUS_CACHE_TTL_SECONDS = 300.0
# Centinela do ICARO (:141-155) para categoria cuja contagem falhou.
CENSUS_FAILED_COUNT = -1

_CATEGORY_QUERY_TMPL = 'category:"%s"'
_CATEGORIES_HINT = (
    "Nem todo build da WebAPI expõe /categories; confirme a versão do "
    "servidor e use iped_search com filtros próprios."
)

# Cache em memória {source|fingerprint: (expira_em, payload)} — precedente de
# fetch.py e do _sources_cache de core/tool.py; entradas são substituídas sob
# lock e nunca mutadas após publicadas (as tools recebem cópias).
_result_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _normalized_categories(raw: list[Any]) -> list[str]:
    """Normaliza o catálogo cru do servidor.

    Args:
        raw: lista recebida de /categories (itens de qualquer tipo JSON).

    Returns:
        Nomes não vazios, sem espaços nas bordas, sem duplicatas, na
        ordem original.
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _fingerprint(categories: list[str]) -> str:
    """Huveda o catálogo de categorias (ordem-insensível).

    Args:
        categories: categorias normalizadas.

    Returns:
        Hex-digest SHA-256 do catálogo ordenado — entra na chave do cache
        para invalidar automaticamente quando a taxonomia muda.
    """
    canonical = json.dumps(sorted(categories), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> dict[str, Any] | None:
    """Lê o cache de censos respeitando o TTL.

    Args:
        key: chave "source|fingerprint".

    Returns:
        Cópia do payload com `cached=True`, ou None se ausente/expirado.
    """
    now = time.monotonic()
    with _cache_lock:
        entry = _result_cache.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if now >= expires_at:
            _result_cache.pop(key, None)
            return None
        return dict(payload, cached=True)


def _cache_put(key: str, payload: dict[str, Any]) -> None:
    """Publica um payload no cache com TTL.

    Args:
        key: chave "source|fingerprint".
        payload: envelope sem a flag `cached` (adicionada na leitura).
    """
    with _cache_lock:
        _result_cache[key] = (time.monotonic() + CENSUS_CACHE_TTL_SECONDS, payload)


def _count_category(client: Any, source_id: str, name: str) -> tuple[str, int, str]:
    """Conta os documentos de UMA categoria; converte falha em dado.

    Args:
        client: `IPEDClient` já resolvido para o source.
        source_id: source do censo.
        name: categoria (nome cru do catálogo).

    Returns:
        (nome, contagem, erro) — `CENSUS_FAILED_COUNT` e a mensagem quando
        a busca desta categoria falha; o pool nunca vê a exceção.
    """
    try:
        groups = client.search(_CATEGORY_QUERY_TMPL % name, source_id=source_id)
        total = sum(len(group.get("ids") or []) for group in groups)
        return name, total, ""
    except (requests.RequestException, ValueError) as exc:
        return name, CENSUS_FAILED_COUNT, str(exc) or type(exc).__name__


def _run_counts(
    client: Any, source_id: str, categories: list[str]
) -> tuple[dict[str, int], list[str]]:
    """Executa as K contagens com concorrência limitada (AC2).

    Args:
        client: `IPEDClient` resolvido.
        source_id: source do censo.
        categories: categorias normalizadas.

    Returns:
        ({categoria: contagem}, [avisos]) — um aviso por categoria que
        falhou, sem ids na mensagem (guarda de contexto).
    """
    counts: dict[str, int] = {}
    warnings: list[str] = []
    if not categories:
        return counts, warnings
    workers = min(CENSUS_WORKERS, len(categories))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_count_category, client, source_id, c) for c in categories]
        for future in as_completed(futures):
            name, count, error = future.result()
            counts[name] = count
            if error:
                warnings.append(f'categoria "{name}": contagem indisponível — {error}')
                logger.warning("Censo %s: %s", source_id, warnings[-1])
    return counts, warnings


def _summary_payload(
    source_id: str, categories: list[str], counts: dict[str, int], warnings: list[str]
) -> dict[str, Any]:
    """Monta o envelope da tool (apenas contagens — guarda de contexto).

    Args:
        source_id: source censado.
        categories: catálogo normalizado.
        counts: contagens por categoria (podem faltar categorias vazias).
        warnings: avisos das categorias que falharam.

    Returns:
        Envelope ordenado por contagem desc (desempate por nome;
        falhas=-1 vão para o fim), sem a flag `cached`.
    """
    entries = sorted(
        ({"name": c, "count": counts.get(c, CENSUS_FAILED_COUNT)} for c in categories),
        key=lambda e: (-e["count"], e["name"]),
    )
    return {
        "source_id": source_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_categories": len(entries),
        "total_docs_counted": sum(e["count"] for e in entries if e["count"] >= 0),
        "categories": entries,
        "warnings": list(warnings),
    }


def build_case_summary(source_id: str) -> dict[str, Any]:
    """Núcleo do censo (lógica testável sem MCP).

    Args:
        source_id: source a censar (obrigatório).

    Returns:
        Envelope de `_summary_payload` com `cached` True/False conforme o
        cache por fingerprint ter poupado (ou não) as N buscas.

    Raises:
        ValueError: source_id vazio, source inexistente/offline (texto
            estável do `Router.resolve`) ou /categories indisponível
            (tradução de `guarded_call`; jar sem o endpoint → 404).
    """
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError("'source_id' é obrigatório (veja iped_list_sources).")
    client = get_router().resolve(sid)
    raw = guarded_call(client.get_categories, "GET /categories", _CATEGORIES_HINT, sid)
    categories = _normalized_categories(raw)
    key = f"{sid}|{_fingerprint(categories)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    counts, warnings = _run_counts(client, sid, categories)
    payload = _summary_payload(sid, categories, counts, warnings)
    _cache_put(key, payload)
    return dict(payload, cached=False)


def case_summary(source_id: str) -> dict[str, Any]:
    """Censo do caso: quantos documentos há em cada categoria (sem ids).

    Use para responder "o que este caso contém?" antes de refinar
    buscas. Lê a taxonomia real (GET /categories) e conta uma busca
    `category:"<categoria>"` por categoria, com concorrência limitada e
    cache (~5 min por source+catálogo). Uma categoria que falha vira
    count=-1 com aviso em `warnings`, sem derrubar o censo. Apenas
    contagens são retornadas — nenhum id de documento entra na resposta.

    Args:
        source_id: source a censar (veja iped_list_sources).

    Returns:
        {source_id, generated_at, cached, total_categories,
        total_docs_counted, categories: [{name, count}] ordenadas por
        contagem desc (count=-1 = contagem que falhou), warnings: [str]}.

    Raises:
        ValueError: source inválido/inexistente ou /categories
            indisponível neste build da WebAPI (jar sem o endpoint).
    """
    return build_case_summary(source_id)


def register(mcp: FastMCP) -> None:
    """Registra a tool de censo da família core no servidor.

    Args:
        mcp: servidor FastMCP ativo.
    """
    mcp.add_tool(case_summary)
