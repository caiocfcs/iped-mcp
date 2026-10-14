"""Família triage: triagem determinística de eixos a partir do índice (CICLO 2).

Ferramentas de "conhecimento de análise" (DESIGN 09): entregam ao agente o
que o fluxo ICARO codificou — categorias canônicas, campos, limiares —
em uma chamada por eixo, com tetos e truncamento marcado. Nenhuma análise
LLM e nenhum acesso a arquivos do caso.

Padrão estrutural do projeto: as tools públicas vivem em ``tool.py``
(nível de módulo) e este ``register(mcp)`` as adiciona ao servidor via
``mcp.add_tool(fn)``.
"""

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from iped_mcp.triage import tool

TRIAGE_TOOLS: tuple[Callable[..., dict[str, Any]], ...] = (
    tool.triage_telephony,
    tool.transcripts_generate,
    tool.triage_identity,
    tool.triage_financial,
    tool.triage_geolocation,
    tool.triage_media,
    tool.triage_devices_networks,
    tool.triage_communication,
    tool.owner_candidates,
    tool.triage_query_discovery,
)


def register(mcp: FastMCP) -> None:
    """Registra as 10 tools da família triage no servidor.

    Args:
        mcp: servidor FastMCP ativo.

    Returns:
        Nada.
    """
    for fn in TRIAGE_TOOLS:
        mcp.add_tool(fn)


__all__ = ["TRIAGE_TOOLS", "register"]
