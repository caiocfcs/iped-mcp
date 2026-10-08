"""Diagnóstico compartilhado de HTTP 500 do IPED."""

from __future__ import annotations

from iped_mcp.router import get_router


def diagnose_500(query: str, hint: str) -> str:
    """HTTP 500: o servidor respondeu (500 é resposta) mas rejeitou a query.

    A checagem de liveness é defensiva (o servidor pode ter caído no
    intervalo). `hint` é a frase final sugerindo como corrigir a query
    (ex.: tool de referência).
    """
    if get_router().online_count() == 0:
        return (
            "HTTP 500 e nenhum servidor IPED está respondendo. "
            "Verifique se o IPED está rodando."
        )
    return (
        "HTTP 500: o servidor IPED está no ar, mas a query foi rejeitada. "
        f"Query montada: {query!r}. {hint}"
    )
