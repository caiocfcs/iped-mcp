"""Estrutura do domínio triage: padrão de registro do projeto (fase 11b).

Fixtures deste arquivo são 100% sintéticas (regra de saneamento): nenhum
dado de investigação real; nenhum fixture é necessário além do servidor
FastMCP local (sem HTTP).
"""

from __future__ import annotations

import asyncio

from fastmcp import Client, FastMCP

from iped_mcp import triage

# Nomes públicos congelados das tools de triagem (contrato externo).
EXPECTED_TOOL_NAMES: list[str] = [
    "owner_candidates",
    "transcripts_generate",
    "triage_communication",
    "triage_devices_networks",
    "triage_financial",
    "triage_geolocation",
    "triage_identity",
    "triage_media",
    "triage_query_discovery",
    "triage_telephony",
]


def test_register_expose_exatamente_as_dez_tools_publicas() -> None:
    """``triage.register(mcp)`` registra as 10 tools com os nomes públicos.

    Teste estrutural anti-deriva: o domínio triage deve manter o padrão
    do projeto (funções públicas em ``tool.py`` adicionadas via
    ``mcp.add_tool`` dentro de um único ``register(mcp)``).

    Returns:
        Nada.
    """
    server = FastMCP("iped_mcp_structure_test")
    triage.register(server)

    async def _names() -> list[str]:
        async with Client(server) as client:
            return sorted(t.name for t in await client.list_tools())

    assert asyncio.run(_names()) == EXPECTED_TOOL_NAMES


def test_funcoes_publicas_moram_em_tool_py() -> None:
    """As 10 funções públicas são definidas em ``iped_mcp.triage.tool``.

    Returns:
        Nada.
    """
    from iped_mcp.triage import tool

    for name in EXPECTED_TOOL_NAMES:
        fn = getattr(tool, name)
        assert callable(fn)
        assert fn.__module__ == "iped_mcp.triage.tool"
        assert fn.__doc__, f"{name} sem docstring (descrição MCP vazia)"
