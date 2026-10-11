"""Taxonomia única de erros de comunicação com a WebAPI do IPED (M-2).

Toda falha de rede/HTTP do `IPEDClient` é traduzida AQUI, na fronteira do
client, antes de vazar para as camadas internas — o texto `requests` cru
("404 Client Error: ... for url: ...") nunca chega ao agente (AC1).

As classes herdam também dos equivalentes de `requests`
(ConnectionError/Timeout/HTTPError/RequestException) por compatibilidade:
catchers antigos fora do escopo do M-2 (ex.: `fetch.py`) continuam
capturando essas falhas normalmente, porém agora com a nossa mensagem
diagnóstica em lugar do texto cru.

Hierarquia (DESIGN 02):

    IpedError
    ├── IpedConnectionError   rede: servidor não respondeu   [requests.ConnectionError]
    │   └── IpedTimeoutError  rede: respondeu devagar         [+requests.Timeout]
    ├── IpedResponseError     respondeu sem texto legível     [requests.RequestException]
    └── IpedHTTPError         status >= 400                   [requests.HTTPError]
        ├── ResourceNotFoundError  404: não existe no servidor
        ├── BadRequestError        4xx restantes — raro: o jar-alvo ignora
        │                          parâmetros inválidos (P2-8); existe para a
        │                          taxonomia de retry do M-3 (4xx nunca retry)
        └── ServerError            5xx: erro interno. No jar-alvo, doc
                                   inexistente responde 500 (2d 10), por isso
                                   o contexto de doc entra na mensagem.

Prefixos estáveis de UX (AC5 — prompts de agentes já calibrados):
"Source '<id>' indisponível", "Source '<id>' não encontrado",
"Timeout no source '<id>'", "Doc não encontrado".
"""

from __future__ import annotations

import requests

# Máximo de caracteres do corpo do servidor anexado à mensagem (snippet do
# erro Grizzly); o corpo integral (HTML gigante) nunca entra em contexto.
_BODY_SNIPPET_LIMIT = 160


# --- mensagens estáveis (AC5) -------------------------------------------------


def source_unavailable_message(source_id: str) -> str:
    """Fonte existe no registry, mas o servidor dela não respondeu (offline)."""
    return (
        f"Source '{source_id}' indisponível: o servidor correspondente "
        "não responde."
    )


def timeout_message(source_id: str) -> str:
    """Timeout numa source específica (texto preservado do router)."""
    return (
        f"Timeout no source '{source_id}': o servidor demorou demais "
        "para responder. Tente novamente ou refine a query."
    )


def source_not_found_message(source_id: str | None = None) -> str:
    """Source não existe nos servidores/registry (texto preservado do router)."""
    who = f" '{source_id}'" if source_id else ""
    return (
        f"Source{who} não encontrado em nenhum endpoint IPED. "
        "Confira o nome com iped_list_sources."
    )


def no_endpoint_online_message(source_id: str) -> str:
    """Todos os servidores mudos: 'indisponível' ≠ 'não existe' (AC2)."""
    return (
        "Source indisponível: nenhum servidor IPED está respondendo; não foi "
        f"possível localizar o source '{source_id}'. Verifique se o IPED está "
        "rodando e repita iped_list_sources."
    )


def doc_not_found_message(
    source_id: str | None, doc_id: int, status_code: int
) -> str:
    """Doc inexistente (prefixo estável AC5); no jar-alvo o caso chega como 500."""
    who = f" no source '{source_id}'" if source_id else ""
    return (
        f"Doc não encontrado{who} (doc {doc_id}): o servidor respondeu "
        f"HTTP {status_code} — doc inexistente ou falha interna. "
        "Confira source_id e doc_id (iped_list_sources / iped_search)."
    )


def server_error_message(source_id: str | None, status_code: int, detail: str) -> str:
    """HTTP 5xx sem contexto de doc: erro interno reportado com snippet do corpo."""
    who = f" do source '{source_id}'" if source_id else ""
    base = f"O servidor IPED{who} respondeu HTTP {status_code}"
    if detail:
        return f"{base}: {detail}"
    return f"{base} (sem texto de erro do servidor)."


def bad_request_message(status_code: int, detail: str) -> str:
    """HTTP 4xx (exceto 404): requisição recusada pelo servidor."""
    base = f"O servidor IPED recusou a requisição (HTTP {status_code})"
    return f"{base}: {detail}" if detail else base


def response_not_readable_message(url: str | None) -> str:
    """Resposta 2xx sem corpo JSON legível (classe 'sem-texto')."""
    where = f" ({url})" if url else ""
    return (
        f"Resposta ilegável do servidor IPED{where}: o corpo não é o JSON "
        "esperado. O serviço pode estar respondendo páginas de erro."
    )


def server_unavailable_text() -> str:
    """Servidor IPED indisponível sem contexto de source (uso em guards)."""
    return "Servidor IPED indisponível. Verifique se o IPED está rodando."


def generic_timeout_text(what: str) -> str:
    """Timeout sem contexto de source, com a requisição envolvida (uso em guards).

    Args:
        what: descrição da requisição que expirou.

    Returns:
        Mensagem estável de timeout.
    """
    return (
        f"Timeout ao consultar o IPED ({what}). "
        "Buscas amplas (ex.: match-all) podem demorar; refine a query."
    )


def _body_snippet(resp: requests.Response) -> str:
    """Primeiros `_BODY_SNIPPET_LIMIT` caracteres do corpo, sem quebras/ABs.

    Args:
        resp: resposta de erro do servidor.

    Returns:
        Snippet de texto limpo; vazio se o corpo não for legível.
    """
    try:
        text = resp.text or ""
    except Exception:  # requests decodifica sob demanda; binário pode falhar
        return ""
    return " ".join(text.split())[:_BODY_SNIPPET_LIMIT]


# --- hierarquia ----------------------------------------------------------------


class IpedError(Exception):
    """Base de toda falha de comunicação com a WebAPI do IPED.

    Attributes:
        url: URL da requisição que falhou (None quando desconhecida).
        source_id: source envolvida (None quando não aplicável).
        status_code: status HTTP (None em falha de rede).
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        source_id: str | None = None,
        status_code: int | None = None,
    ) -> None:
        """Armazena a mensagem diagnóstica e o contexto da requisição.

        Args:
            message: texto estável apresentado ao agente.
            url: URL da requisição que falhou.
            source_id: source envolvida, quando conhecida.
            status_code: status HTTP, quando houve resposta.
        """
        super().__init__(message)
        self.url = url
        self.source_id = source_id
        self.status_code = status_code


class IpedConnectionError(IpedError, requests.ConnectionError):
    """Endpoint offline: conexão recusada/inexistente — o servidor não respondeu."""


class IpedTimeoutError(IpedConnectionError, requests.Timeout):
    """O servidor não respondeu a tempo (connect ou read timeout)."""


class IpedResponseError(IpedError, requests.RequestException):
    """O servidor respondeu, mas sem o texto/JSON esperado (sem-texto)."""


class IpedHTTPError(IpedError, requests.HTTPError):
    """HTTP >= 400: o servidor respondeu com erro (carrega a `Response`)."""

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        source_id: str | None = None,
        status_code: int | None = None,
        response: requests.Response | None = None,
    ) -> None:
        """Monta o erro HTTP com mensagem traduzida e a resposta anexada.

        Args:
            message: texto estável apresentado ao agente.
            url: URL da requisição que falhou.
            source_id: source envolvida, quando conhecida.
            status_code: status HTTP; inferido de `response` quando omitido.
            response: resposta original (M-8 consome o corpo do 500 de /text).
        """
        # Chamada direta ao __init__ do pai requests: define .response/.request
        # no contrato do HTTPError sem a cadeia cooperativa de kwargs.
        requests.HTTPError.__init__(self, message, response=response)
        self.url = url
        self.source_id = source_id
        self.status_code = (
            status_code
            if status_code is not None
            else (response.status_code if response is not None else None)
        )


class ResourceNotFoundError(IpedHTTPError):
    """HTTP 404: o recurso nomeado na URL não existe no servidor."""


class BadRequestError(IpedHTTPError):
    """HTTP 4xx (exceto 404): requisição recusada — nunca retryável (M-3)."""


class ServerError(IpedHTTPError):
    """HTTP 5xx: erro interno (doc inexistente responde 500 no jar-alvo)."""


# --- fábricas (tradução centralizada) ------------------------------------------


def translate_network_error(
    exc: requests.RequestException,
    *,
    url: str | None = None,
    source_id: str | None = None,
) -> IpedError:
    """Converte exceção de rede do requests na classe da taxonomia.

    Args:
        exc: exceção capturada da sessão (Timeout/ConnectionError/outra).
        url: URL da requisição.
        source_id: source envolvida, quando conhecida.

    Returns:
        Instância pronta para `raise` (mensagem com prefixo estável).
    """
    if isinstance(exc, requests.Timeout):
        if source_id:
            return IpedTimeoutError(
                timeout_message(source_id), url=url, source_id=source_id
            )
        return IpedTimeoutError(
            "Timeout ao consultar o IPED. Buscas amplas (ex.: match-all) "
            "podem demorar; refine a query.",
            url=url,
        )
    if isinstance(exc, requests.ConnectionError):
        if source_id:
            return IpedConnectionError(
                source_unavailable_message(source_id), url=url, source_id=source_id
            )
        return IpedConnectionError(
            "Servidor IPED indisponível. Verifique se o IPED está rodando.",
            url=url,
        )
    return IpedConnectionError(
        "Servidor IPED indisponível. Verifique se o IPED está rodando. "
        f"Detalhe: {exc}",
        url=url,
        source_id=source_id,
    )


def error_for_response(
    resp: requests.Response,
    *,
    url: str | None = None,
    source_id: str | None = None,
    doc_id: int | None = None,
) -> IpedHTTPError:
    """Constrói a exceção da taxonomia para uma resposta de erro (>= 400).

    Args:
        resp: resposta com status de erro.
        url: URL da requisição.
        source_id: source envolvida, quando conhecida.
        doc_id: doc envolvido; marca endpoint de documento (onde o jar-alvo
            responde 500 para doc inexistente — 2d 10).

    Returns:
        Instância de `IpedHTTPError` (ou subclasse) pronta para `raise`.
    """
    status = resp.status_code
    detail = _body_snippet(resp)
    if status == 404:
        message = (
            doc_not_found_message(source_id, doc_id, status)
            if doc_id is not None
            else source_not_found_message(source_id)
        )
        klass: type[IpedHTTPError] = ResourceNotFoundError
    elif 400 <= status < 500:
        message = bad_request_message(status, detail)
        klass = BadRequestError
    else:
        message = (
            doc_not_found_message(source_id, doc_id, status)
            if doc_id is not None
            else server_error_message(source_id, status, detail)
        )
        klass = ServerError
    return klass(
        message,
        url=url,
        source_id=source_id,
        status_code=status,
        response=resp,
    )
