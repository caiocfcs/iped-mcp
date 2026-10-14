"""Família core: tools genéricas sobre a WebAPI + censo de caso (client-side)."""

from fastmcp import FastMCP

from .census import register as register_census
from .tool import register as register_tools


def register(mcp: FastMCP) -> None:
    """Registra as tools da família core no servidor.

    Args:
        mcp: servidor FastMCP ativo.
    """
    register_tools(mcp)
    register_census(mcp)


__all__ = ["register"]
