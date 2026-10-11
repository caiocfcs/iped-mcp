"""Censo de caso client-side + GET /categories no client (M-6 / DESIGN 06).

Camadas: client.get_categories (envelope {"data"}, jar sem endpoint -> 404
da taxonomia, envelope malformado) e a tool case_summary: contagens por
categoria (AC1), fonte sem categorias (AC1), categoria 0 hits, fallback
count=-1 em falha de 1 das N buscas com o resto intacto (AC3), exatamente
1 /categories + K buscas com concorrencia <= CENSUS_WORKERS (AC2), cache
por fingerprint que pula as N buscas no TTL (AC5) e guarda de contexto
(nenhum id no envelope). Registro pelo caminho real MCP in-process.
Nenhum acesso a rede externa.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from iped_mcp.core import census
from iped_mcp.core.census import case_summary
from iped_mcp import errors
from tests.conftest import ALL_DOC_IDS, SOURCE_ID, UNKNOWN_SOURCE

SUMMARY_KEYS = {
    "source_id",
    "generated_at",
    "cached",
    "total_categories",
    "total_docs_counted",
    "categories",
    "warnings",
}


def _setup(api: Any, categories: list[str], hits: dict[str, list[int]] | None = None) -> None:
    """Preenche a taxonomia do fake e as contagens por categoria.

    Args:
        api: estado observavel do FakeWebAPI.
        categories: catalogo do GET /categories.
        hits: {categoria: [ids]} casados pelas buscas category:"<cat>".
    """
    api.categories = list(categories)
    api.category_hits = dict(hits or {})


def _log_paths(api: Any, path: str) -> list[dict[str, Any]]:
    """Pedidos do log do fake cujo caminho (decodificado) e `path`."""
    return [e for e in api.requests_log if e["path"] == path]


# --- client.get_categories ------------------------------------------------


def test_client_get_categories_happy(webapi: Any, client: Any) -> None:
    """GET /categories devolve a lista do envelope {"data"} na ordem."""
    _setup(webapi, ["email", "imagem"])
    assert client.get_categories(SOURCE_ID) == ["email", "imagem"]
    calls = _log_paths(webapi, "/categories")
    assert len(calls) == 1 and calls[0]["method"] == "GET"


def test_client_get_categories_jar_sem_endpoint(webapi: Any, client: Any) -> None:
    """Jar sem /categories (404 text/plain, padrão 2d) -> taxonomia M-2."""
    webapi.has_categories_endpoint = False
    with pytest.raises(errors.ResourceNotFoundError):
        client.get_categories(SOURCE_ID)


def test_client_get_categories_envelope_malformado(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resposta sem a lista "data" e falha de contrato, nao TypeError."""
    monkeypatch.setattr(client, "_get", lambda *a, **k: {"nope": True})
    with pytest.raises(errors.IpedResponseError):
        client.get_categories(SOURCE_ID)


# --- tool case_summary -----------------------------------------------------


def test_tool_contagens_por_categoria(fake_router: Any, webapi: Any) -> None:
    """AC1: contagem por categoria correta, ordenada por count desc."""
    _setup(webapi, ["email", "imagem"], {"email": ALL_DOC_IDS[:2], "imagem": [ALL_DOC_IDS[0]]})
    result = case_summary(SOURCE_ID)
    assert result["cached"] is False
    assert result["source_id"] == SOURCE_ID
    assert result["total_categories"] == 2
    assert result["total_docs_counted"] == 3
    assert result["categories"] == [
        {"name": "email", "count": 2},
        {"name": "imagem", "count": 1},
    ]
    assert result["warnings"] == []
    assert set(result) == SUMMARY_KEYS


def test_tool_categoria_zero_hits(fake_router: Any, webapi: Any) -> None:
    """Categoria sem hits conta 0 (0 hits mudo do jar, WI-16) e nao falha."""
    _setup(webapi, ["email", "vazia"], {"email": list(ALL_DOC_IDS)})
    result = case_summary(SOURCE_ID)
    assert result["categories"][-1] == {"name": "vazia", "count": 0}
    assert result["total_docs_counted"] == len(ALL_DOC_IDS)
    assert result["warnings"] == []


def test_tool_sem_categorias_nao_faz_buscas(fake_router: Any, webapi: Any) -> None:
    """AC1 caso vazio: catalogo vazio -> censo vazio, zero buscas."""
    _setup(webapi, [])
    result = case_summary(SOURCE_ID)
    assert result["categories"] == []
    assert result["total_categories"] == 0
    assert result["total_docs_counted"] == 0
    assert _log_paths(webapi, "/search") == []


def test_tool_falha_parcial_count_menos_um(fake_router: Any, webapi: Any) -> None:
    """AC3: 500 em 1 das N buscas -> count=-1 + warning; resto intacto."""
    _setup(
        webapi,
        ["email", "imagem"],
        {"email": list(ALL_DOC_IDS), "imagem": [ALL_DOC_IDS[0]]},
    )
    webapi.fail_search_queries = {'category:"email"'}
    result = case_summary(SOURCE_ID)
    by_name = {c["name"]: c["count"] for c in result["categories"]}
    assert by_name["email"] == census.CENSUS_FAILED_COUNT
    assert by_name["imagem"] == 1
    assert result["total_docs_counted"] == 1
    assert len(result["warnings"]) == 1
    assert "email" in result["warnings"][0]


def test_tool_envelope_sem_ids(fake_router: Any, webapi: Any) -> None:
    """Guarda de contexto: nenhum id de documento sai no envelope."""
    _setup(webapi, ["email"], {"email": list(ALL_DOC_IDS)})
    serialized = json.dumps(case_summary(SOURCE_ID), ensure_ascii=False)
    assert "ids" not in serialized
    for entry in json.loads(serialized)["categories"]:
        assert set(entry) == {"name", "count"}


def test_tool_k_buscas_concorrencia_limitada(fake_router: Any, webapi: Any) -> None:
    """AC2: 1 /categories + K buscas; concorrencia <= CENSUS_WORKERS."""
    _setup(webapi, [f"cat{i}" for i in range(6)])
    webapi.search_latency_s = 0.05
    assert case_summary(SOURCE_ID)["total_categories"] == 6
    assert len(_log_paths(webapi, "/categories")) == 1
    searches = _log_paths(webapi, "/search")
    assert len(searches) == 6
    for entry in searches:
        assert entry["query"]["q"][0].startswith('category:"')
    assert census.CENSUS_WORKERS == 4
    assert 2 <= webapi.search_peak_inflight <= census.CENSUS_WORKERS


def test_tool_cache_por_fingerprint(fake_router: Any, webapi: Any) -> None:
    """AC5: reexecucao no TTL pula as N buscas; taxonomia nova re-censa."""
    _setup(webapi, ["email", "imagem"], {"email": list(ALL_DOC_IDS)})
    case_summary(SOURCE_ID)
    searches_first = len(_log_paths(webapi, "/search"))
    second = case_summary(SOURCE_ID)
    assert second["cached"] is True
    assert len(_log_paths(webapi, "/search")) == searches_first
    webapi.categories = ["email", "imagem", "documento"]
    third = case_summary(SOURCE_ID)
    assert third["cached"] is False
    assert len(_log_paths(webapi, "/search")) == searches_first + 3


def test_tool_categories_404_vira_erro_estavel(fake_router: Any, webapi: Any) -> None:
    """Jar sem /categories: ValueError traduzido (nao censo falso vazio)."""
    _setup(webapi, ["email"])
    webapi.has_categories_endpoint = False
    with pytest.raises(ValueError, match="não encontrado"):
        case_summary(SOURCE_ID)


def test_tool_validacoes_de_source(fake_router: Any) -> None:
    """source_id vazio e source inexistente: ValueError sem HTTP."""
    with pytest.raises(ValueError, match="source_id"):
        case_summary("  ")
    with pytest.raises(ValueError):
        case_summary(UNKNOWN_SOURCE)


# --- registro no servidor (caminho real MCP in-process) --------------------


def test_tool_registrada_via_mcp(fake_router: Any, webapi: Any) -> None:
    """case_summary e descoberta e chamada pelo MCP (mesmo mecanismo)."""
    from fastmcp import Client, FastMCP

    from iped_mcp.core import register as register_core

    _setup(webapi, ["email"], {"email": list(ALL_DOC_IDS)})
    server = FastMCP("iped_mcp_test")
    register_core(server)

    async def _run() -> dict[str, Any]:
        async with Client(server) as client:
            names = [t.name for t in await client.list_tools()]
            assert "case_summary" in names
            result = await client.call_tool("case_summary", {"source_id": SOURCE_ID})
            return result.data if result.data is not None else json.loads(result.content[0].text)

    payload = asyncio.run(_run())
    assert payload["categories"] == [{"name": "email", "count": len(ALL_DOC_IDS)}]
