"""Família triage: triagem determinística de eixos a partir do índice (CICLO 2).

Ferramentas de "conhecimento pericial" (DESIGN 09): entregam ao agente o
que o fluxo ICARO codificou — categorias canônicas, campos, limiares —
em uma chamada por eixo, com tetos e truncamento marcado. Nenhuma análise
LLM e nenhum acesso a arquivos do caso.
"""

from fastmcp import FastMCP

from iped_mcp.triage.telephony import register as register_telephony


def register(mcp: FastMCP) -> None:
    """Registra as tools da família triage no servidor.

    Args:
        mcp: servidor FastMCP ativo.
    """
    register_telephony(mcp)


__all__ = ["register"]
