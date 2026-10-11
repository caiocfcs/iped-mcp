"""Família triage: triagem determinística de eixos a partir do índice (CICLO 2).

Ferramentas de "conhecimento pericial" (DESIGN 09): entregam ao agente o
que o fluxo ICARO codificou — categorias canônicas, campos, limiares —
em uma chamada por eixo, com tetos e truncamento marcado. Nenhuma análise
LLM e nenhum acesso a arquivos do caso.
"""

from fastmcp import FastMCP

from iped_mcp.triage.financial import register as register_financial
from iped_mcp.triage.geolocation import register as register_geolocation
from iped_mcp.triage.devices_networks import register as register_devices_networks
from iped_mcp.triage.identity import register as register_identity
from iped_mcp.triage.media import register as register_media
from iped_mcp.triage.telephony import register as register_telephony
from iped_mcp.triage.transcripts import register as register_transcripts


def register(mcp: FastMCP) -> None:
    """Registra as tools da família triage no servidor.

    Args:
        mcp: servidor FastMCP ativo.

    Returns:
        Nada.
    """
    register_financial(mcp)
    register_geolocation(mcp)
    register_identity(mcp)
    register_media(mcp)
    register_devices_networks(mcp)
    register_telephony(mcp)
    register_transcripts(mcp)


__all__ = ["register"]
