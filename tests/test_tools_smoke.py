"""Smoke das 5 familias de tools via cliente MCP in-process (fastmcp).

Cada teste exerce UMA tool representativa da familia pelo caminho real
(MCP -> tool -> Router -> IPEDClient -> fake 2d). Nenhum acesso a rede
externa (AC5/AC6 do Design 01): o backend e o fake da porta efemera.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tests.conftest import (
    ALL_DOC_IDS,
    DOC_MD5_UPPER,
    DOC_NO_IMAGE_ID,
    EXISTING_BOOKMARK,
    SOURCE_ID,
    SOURCE_ID_2,
    SOURCE_PATH,
)

WA_TOOL_TIMEOUT_NOTE = "whatsapp_overview com 0 docs WA curta por design (_NO_WA_RESULT)"


@pytest.fixture()
def mcp_server(fake_router: Any) -> Any:
    """Servidor FastMCP com as 5 familias registradas (espele main.py)."""
    from fastmcp import FastMCP

    from iped_mcp.bookmarks import register as register_bookmarks
    from iped_mcp.core import register as register_core
    from iped_mcp.regex import register as register_regex
    from iped_mcp.ufed import register as register_ufed
    from iped_mcp.whatsapp import register as register_whatsapp

    server = FastMCP("iped_mcp_test")
    register_core(server)
    register_ufed(server)
    register_regex(server)
    register_bookmarks(server)
    register_whatsapp(server)
    return server


@pytest.fixture()
def call_tool(mcp_server: Any) -> Any:
    """Devolve callable(name, args) -> payload Python da tool via MCP in-process.

    Args:
        mcp_server: Servidor FastMCP da fixture acima.

    Returns:
        Funcao que roda um `Client(server).call_tool` em asyncio.run e
        desempacota o resultado (.data quando presente, senao JSON do text).
    """

    def _call(name: str, args: dict[str, Any] | None = None) -> Any:
        from fastmcp import Client

        async def _run() -> Any:
            async with Client(mcp_server) as client:
                return await client.call_tool(name, args or {})

        result = asyncio.run(_run())
        data = getattr(result, "data", None)
        if data is not None:
            return data
        return json.loads(result.content[0].text)

    return _call


def test_core_family_smoke(call_tool: Any) -> None:
    """core: list_sources + search atravessam MCP -> Router -> fake (WI-1/WI-20).

    M-2/AC3: list_sources devolve dados + falha por endpoint offline
    (offline_endpoints vazio quando o fake unico responde).
    """
    listing = call_tool("list_sources")
    assert listing["offline_endpoints"] == []
    sources = listing["sources"]
    assert [s["id"] for s in sources] == [SOURCE_ID, SOURCE_ID_2]
    assert sources[0]["path"] == SOURCE_PATH

    result = call_tool("search", {"query": "*", "source_id": SOURCE_ID})
    assert result["total"] == len(ALL_DOC_IDS)
    assert result["ids"][: len(ALL_DOC_IDS)] == ALL_DOC_IDS


def test_ufed_family_smoke(call_tool: Any) -> None:
    """ufed: ufed_search(MD5) monta `ufed\\:MD5:...` e resolve docs pelo fake (WI-16 formato)."""
    result = call_tool(
        "ufed_search",
        {"source_id": SOURCE_ID, "attribute": "MD5", "value": DOC_MD5_UPPER},
    )
    assert result["total"] == 1
    assert result["docs"][0]["id"] == DOC_NO_IMAGE_ID


def test_regex_family_smoke(call_tool: Any) -> None:
    """regex: regex_stats(EMAIL) faz search+fetch e conta o valor da prop (WI-17)."""
    result = call_tool("regex_stats", {"source_id": SOURCE_ID, "attribute": "EMAIL"})
    assert result["total_docs"] == 1
    assert result["total_matches"] == 1


def test_bookmarks_family_smoke(call_tool: Any) -> None:
    """bookmarks: bookmarks_list roteia pelo source_id e devolve os nomes."""
    assert call_tool("bookmarks_list", {"source_id": SOURCE_ID}) == [EXISTING_BOOKMARK]


def test_whatsapp_family_smoke(call_tool: Any) -> None:
    """whatsapp: overview com 0 docs WA encerra no contrato _NO_WA_RESULT.

    WA_TOOL_TIMEOUT_NOTE: sem indice WhatsApp o caminho curto e o esperado
    (2d 3a); os fluxos com dados ficam como TO_VERIFY do Design 01 (PW-x).
    """
    result = call_tool("whatsapp_overview", {"source_id": SOURCE_ID})
    assert result["has_whatsapp"] is False
    assert result["total_wa"] == 0
