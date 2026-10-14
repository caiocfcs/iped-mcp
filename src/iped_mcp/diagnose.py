"""Política única de tradução de erros para as tools do IPED (DESIGN 02).

As mensagens com prefixos estáveis (UX já calibrada pelos agentes) são
montadas em `iped_mcp.errors`; este módulo concentra a tradução que
depende do estado do router (liveness) e o guard compartilhado que
substitui as três variantes antes espalhadas por core/ufed/bookmarks
(2a §). `diagnose_500` continua disponível para `fetch.search_ids`, que
trata o 500 de query na hora (sem retry).
"""

from __future__ import annotations

from typing import Any, Callable

import requests

from iped_mcp.errors import server_unavailable_text, generic_timeout_text
from iped_mcp.router import get_router


def diagnose_500(query: str, hint: str) -> str:
    """HTTP 500: o servidor respondeu (500 é resposta) mas rejeitou a query.

    A checagem de liveness é defensiva (o servidor pode ter caído no
    intervalo). `hint` é a frase final sugerindo como corrigir a query
    (ex.: tool de referência).

    Args:
        query: texto da query/requisição montada (entra no diagnóstico).
        hint: frase final sugerindo como corrigir.

    Returns:
        Mensagem diagnóstica estável para o agente.
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


def _http_failure_text(exc: requests.RequestException, what: str, hint: str) -> str:
    """Texto de erro para uma falha HTTP já traduzida pelo client.

    Args:
        exc: exceção de transporte capturada (possui `.response` quando HTTP).
        what: descrição da requisição (query montada) para o diagnóstico 500.
        hint: frase final sugerindo como corrigir.

    Returns:
        Mensagem para o `ValueError` apresentado ao agente.
    """
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    if status == 500:
        return diagnose_500(what, hint)
    message = str(exc).strip()
    if message:
        return message
    return f"Falha na comunicação com o IPED ({what})."


def guarded_call(
    fn: Callable[..., Any], what: str, hint: str, *args: Any, **kwargs: Any
) -> Any:
    """Executa `fn` traduzindo erros IPED residuais em ValueError com diagnóstico.

    Política única (DESIGN 02): HTTP 500 → diagnóstico de query rejeitada com
    checagem de liveness; demais erros HTTP → mensagem traduzida do próprio
    erro (prefixos estáveis de `iped_mcp.errors`); rede/timeout → texto
    estável de indisponibilidade. `ValueError` (validação de tool) passa
    direto, intocado.

    Args:
        fn: callable a executar (tipicamente um método do router).
        what: descrição da requisição para o diagnóstico (ex.: query montada).
        hint: frase final sugerindo como corrigir (contexto da tool).
        *args: posicionais repassados a `fn`.
        **kwargs: keyword repassados a `fn`.

    Returns:
        O resultado de `fn(*args, **kwargs)`.

    Raises:
        ValueError: qualquer falha de comunicação com o IPED.
    """
    try:
        return fn(*args, **kwargs)
    except ValueError:
        raise
    except requests.ConnectionError as exc:
        raise ValueError(server_unavailable_text()) from exc
    except requests.Timeout as exc:
        raise ValueError(generic_timeout_text(what)) from exc
    except requests.RequestException as exc:
        raise ValueError(_http_failure_text(exc, what, hint)) from exc
