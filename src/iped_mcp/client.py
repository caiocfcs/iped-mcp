"""Cliente para a API REST do IPED.

Toda falha de rede/HTTP é traduzida na fronteira deste módulo para as
classes de `iped_mcp.errors` (política única de erro, DESIGN 02): as
camadas internas nunca veem exceções `requests` cruas nem o texto
"404 Client Error: ... for url: ...".

Toda requisição passa pela política de resiliência do DESIGN 03 (M-3):
retry ×3 com backoff 2^n somente para falhas transitórias (conexão,
timeout, 5xx — nunca 4xx; mutações só em falha de conexão pré-resposta)
e circuit breaker por base_url com sonda `GET /sources/`. Como o cliente
é um por endpoint, o breaker "por base_url" é naturalmente um por
instância. O teto de tempo de uma chamada é
`tentativas × timeout + backoffs` (ver `iped_mcp.resilience`).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv

from iped_mcp.config import CB_PROBE_TIMEOUT_SECONDS, WORKERS
from iped_mcp.errors import (
    IpedConnectionError,
    IpedError,
    IpedHTTPError,
    IpedResponseError,
    error_for_response,
    response_not_readable_message,
    server_unavailable_text,
    translate_network_error,
)
from iped_mcp.resilience import CircuitBreaker, RetryPolicy

logger = logging.getLogger("iped_mcp.client")

load_dotenv()

DEFAULT_IPED_URL = os.environ.get("IPED_URL", "http://localhost:1234")
# Timeout de leitura (entre bytes) para download de arquivos; arquivos grandes
# em conexões lentas podem levar mais que o timeout padrão.
_CONTENT_READ_TIMEOUT = 120.0
# Par (connect, read) dos endpoints de conteúdo (content/text/thumb).
_STREAM_TIMEOUT = (10.0, _CONTENT_READ_TIMEOUT)
# Teto de leitura do /text (chunked, sem Content-Length): evita estourar memória
# em docs com texto extraído gigante. Acima disso, capped=True.
_TEXT_READ_CAP = 10 * 1024 * 1024  # 10 MB
# Pool de conexões casado com o paralelismo de fetch (config.WORKERS): com o
# pool padrão (10), workers > 10 descartam conexões ("pool is full") e parte
# dos requests paga um novo handshake TCP. +8 de folga p/ buscas concorrentes.
_POOL_SIZE = WORKERS + 8
# Tamanho do chunk de leitura do /text (iter_content).
_TEXT_CHUNK_SIZE = 65536


class IPEDClient:
    """Cliente para a API REST do IPED (um endpoint/base_url).

    Superfície inalterada para os chamadores; retry, circuit breaker e a
    tradução de erro são transparentes (DESIGN 02 + DESIGN 03).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_IPED_URL,
        timeout: float = 10,
        *,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Cria o cliente com pool de conexões e política de resiliência.

        Args:
            base_url: URL base da WebAPI (sem barra final).
            timeout: timeout padrão de requisição, em segundos.
            session: sessão requests (injeção para teste); por padrão cria
                uma com pool dimensionado para o fetch paralelo.
            sleeper: função de espera do backoff (injeção para teste);
                padrão `time.sleep`, resolvido na construção.
            clock: relógio monotônico do circuit breaker (injeção p/ teste);
                padrão `time.monotonic`, resolvido na construção.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._stream_timeout = (self.timeout, _CONTENT_READ_TIMEOUT)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        self._session = session
        # Defaults de tempo resolvidos na construção (via atributo do
        # módulo): permite ao conftest zerar o backoff da suíte sem tocar
        # nos testes que injetam sleeper/clock próprios.
        self._retry_policy = RetryPolicy(
            sleep=sleeper if sleeper is not None else time.sleep
        )
        self._breaker = CircuitBreaker(
            name=self.base_url,
            probe=self._probe_endpoint,
            clock=clock if clock is not None else time.monotonic,
        )

    # --- fronteira única de tradução de erro (DESIGN 02) ----------------------
    # --- e única aplicação da política de resiliência (DESIGN 03) -------------

    def _probe_endpoint(self) -> bool:
        """Sonda de vida do breaker: `GET /sources/` com timeout curto (M-3).

        Bypassa deliberadamente o retry e o circuito — ela É a decisão de
        reabertura (padrão ping do icaro, iped_client.py:65-73).

        Returns:
            True se o servidor respondeu HTTP 200.
        """
        try:
            resp = self._session.request(
                "GET",
                f"{self.base_url}/sources/",
                timeout=CB_PROBE_TIMEOUT_SECONDS,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | tuple[float, float] | None = None,
        source_id: str | None = None,
        doc_id: int | None = None,
        retry_server_errors: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        """Executa a requisição sob a política de resiliência e traduz falhas.

        Circuito aberto → falha rápida, sem tocar a rede. Fechado → até
        `RetryPolicy.max_attempts` tentativas para falhas transitórias
        (nunca 4xx; mutações só em falha de conexão pré-resposta).

        Args:
            method: verbo HTTP ("GET", "POST", "PUT", "DELETE").
            path: caminho relativo à base_url.
            timeout: segundos, ou par (connect, read); padrão `self.timeout`.
            source_id: source envolvida (contexto da mensagem de erro).
            doc_id: doc envolvido; marca endpoint de documento, onde o
                jar-alvo responde 500 para doc inexistente (2d 10).
            retry_server_errors: False em rotas cujo 5xx é determinístico
                (`search`: query rejeitada → diagnóstico imediato, AC4).
            **kwargs: repassados ao requests (params, json...).

        Returns:
            A resposta, quando o status é < 400.

        Raises:
            IpedConnectionError: servidor não respondeu (offline/recusa) ou
                circuito aberto (fail-fast).
            IpedTimeoutError: servidor não respondeu a tempo.
            IpedHTTPError: status >= 400 (subclasse por classe de status).
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        self._breaker.guard()
        return self._execute_with_retry(
            method,
            url,
            timeout=timeout if timeout is not None else self.timeout,
            source_id=source_id,
            doc_id=doc_id,
            retry_server_errors=retry_server_errors,
            **kwargs,
        )

    def _execute_with_retry(
        self,
        method: str,
        url: str,
        *,
        timeout: float | tuple[float, float],
        source_id: str | None,
        doc_id: int | None,
        retry_server_errors: bool,
        **kwargs: Any,
    ) -> requests.Response:
        """Loop de tentativas com backoff; a decisão é do `RetryPolicy`.

        Args:
            method: verbo HTTP.
            url: URL absoluta da requisição.
            timeout: segundos ou par (connect, read).
            source_id: source envolvida (contexto do erro).
            doc_id: doc envolvido (contexto do erro).
            retry_server_errors: ver `_request`.
            **kwargs: repassados ao requests.

        Returns:
            A resposta da primeira tentativa bem-sucedida (< 400).

        Raises:
            IpedError: a última falha traduzida, esgotadas as tentativas,
                ou a primeira falha não re-tentável.
        """
        last_exc: IpedError | None = None
        for attempt in range(self._retry_policy.max_attempts):
            try:
                resp = self._attempt(
                    method,
                    url,
                    timeout=timeout,
                    source_id=source_id,
                    doc_id=doc_id,
                    **kwargs,
                )
            except IpedError as exc:
                if isinstance(exc, IpedHTTPError):
                    # O servidor RESPONDEU (mesmo com erro): rede viva p/
                    # o breaker — só falha de conexão pontua (icaro :93).
                    self._breaker.record_success()
                if not self._retry_policy.should_retry(
                    exc, method, retry_server_errors=retry_server_errors
                ):
                    raise
                last_exc = exc
                logger.warning(
                    "Tentativa %d/%d falhou em %s (%s); re-tentando.",
                    attempt + 1,
                    self._retry_policy.max_attempts,
                    url,
                    exc,
                )
                if attempt + 1 < self._retry_policy.max_attempts:
                    self._retry_policy.sleep(self._retry_policy.backoff(attempt))
            else:
                self._breaker.record_success()
                return resp
        if last_exc is None:  # defensivo: max_attempts >= 1 garante exceção
            raise IpedConnectionError(server_unavailable_text(), url=url)
        if isinstance(last_exc, IpedConnectionError):
            # Esgotou por falha de conexão: única classe que pontua o breaker.
            self._breaker.record_connection_failure()
        raise last_exc

    def _attempt(
        self,
        method: str,
        url: str,
        *,
        timeout: float | tuple[float, float],
        source_id: str | None,
        doc_id: int | None,
        **kwargs: Any,
    ) -> requests.Response:
        """Uma tentativa HTTP: resposta < 400 ou erro da taxonomia (M-2).

        Args:
            method: verbo HTTP.
            url: URL absoluta.
            timeout: segundos ou par (connect, read).
            source_id: source envolvida (contexto do erro).
            doc_id: doc envolvido (contexto do erro).
            **kwargs: repassados ao requests.

        Returns:
            A resposta com status < 400.

        Raises:
            IpedError: falha de rede ou status >= 400, já traduzidos.
        """
        try:
            resp = self._session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise translate_network_error(exc, url=url, source_id=source_id) from exc
        if resp.status_code >= 400:
            raise error_for_response(resp, url=url, source_id=source_id, doc_id=doc_id)
        return resp

    @staticmethod
    def _json(resp: requests.Response) -> Any:
        """Decodifica o corpo JSON; corpo ilegável vira `IpedResponseError`.

        Args:
            resp: resposta 2xx do servidor.

        Returns:
            O payload JSON decodificado.

        Raises:
            IpedResponseError: corpo 2xx sem JSON legível (classe sem-texto).
        """
        try:
            return resp.json()
        except ValueError as exc:
            raise IpedResponseError(
                response_not_readable_message(resp.url), url=resp.url
            ) from exc

    def _get(
        self,
        path: str,
        *,
        timeout: float | tuple[float, float] | None = None,
        source_id: str | None = None,
        doc_id: int | None = None,
        retry_server_errors: bool = True,
        **kwargs: Any,
    ) -> Any:
        """GET + JSON com tradução centralizada e resiliência (ver `_request`).

        Args:
            path: caminho relativo à base_url.
            timeout: segundos ou par (connect, read).
            source_id: source envolvida (contexto do erro).
            doc_id: doc envolvido (contexto do erro).
            retry_server_errors: ver `_request`.
            **kwargs: repassados ao requests.

        Returns:
            O payload JSON da resposta.

        Raises:
            IpedError: qualquer falha de comunicação (classes da taxonomia).
        """
        resp = self._request(
            "GET",
            path,
            timeout=timeout,
            source_id=source_id,
            doc_id=doc_id,
            retry_server_errors=retry_server_errors,
            **kwargs,
        )
        return self._json(resp)

    # --- superfície pública -----------------------------------------------------

    def list_sources(self, timeout: float | None = None) -> list[dict[str, Any]]:
        """Lista os sources registrados no servidor IPED.

        Args:
            timeout: timeout desta chamada (padrão do cliente se None).

        Returns:
            Lista de sources (dicts com `id` e `path`).

        Raises:
            IpedError: falha de rede/HTTP (taxonomia `iped_mcp.errors`).
        """
        return self._get("sources/", timeout=timeout)["data"]

    def search(
        self,
        query: str,
        source_id: str | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Busca documentos por query Lucene, opcionalmente restrita a um source.

        O 500 do /search é determinístico (query rejeitada pelo servidor),
        por isso NÃO é re-tentado: re-tentar a mesma query só custaria o
        backoff antes do mesmo diagnóstico (AC4; 500-imediato do fetch).

        Args:
            query: query Lucene.
            source_id: source a restringir a busca (None = todos do servidor).
            timeout: timeout desta chamada.

        Returns:
            Grupos [{source, ids}] — sem paginação server-side (WI-1).

        Raises:
            IpedError: falha de rede/HTTP (taxonomia `iped_mcp.errors`).
        """
        params: dict[str, str] = {"q": query}
        if source_id:
            params["sourceID"] = source_id
        return self._get(
            "search",
            params=params,
            timeout=timeout,
            source_id=source_id,
            retry_server_errors=False,
        )["data"]

    def get_document(self, source_id: str, doc_id: int) -> dict[str, Any]:
        """Retorna o documento (incluindo `properties`).

        Args:
            source_id: source do doc.
            doc_id: id do doc.

        Returns:
            Doc completo (properties no topo; contrato do endpoint #3).

        Raises:
            IpedError: falha de rede/HTTP; 404/500 de doc inexistente chegam
                como `ResourceNotFoundError`/`ServerError` com prefixo
                "Doc não encontrado".
        """
        return self._get(
            f"sources/{source_id}/docs/{doc_id}", source_id=source_id, doc_id=doc_id
        )

    # --- Bookmarks (marcadores) ---
    # Tudo dá HTTP 200; mutações (POST/PUT/DELETE) retornam body vazio —
    # checar status_code, nunca resp.json(). Nomes com espaço exigem quote().
    # Resiliência (M-3): mutações só re-tentam falha de CONEXÃO (pré-resposta);
    # timeout/5xx em mutação = resposta ambígua → sobe na hora.

    def list_bookmarks(self) -> list[str]:
        """Lista os nomes dos bookmarks.

        Returns:
            Nomes de bookmark do servidor.

        Raises:
            IpedError: falha de rede/HTTP (taxonomia `iped_mcp.errors`).
        """
        return self._get("bookmarks")["data"]

    def get_bookmark_docs(self, bookmark: str) -> list[dict[str, Any]]:
        """Docs do bookmark agrupados por source: [{"source": ..., "ids": [...]}].

        Args:
            bookmark: nome do bookmark.

        Returns:
            Grupos de docs; bookmark inexistente → [] (HTTP 200).

        Raises:
            IpedError: falha de rede/HTTP (taxonomia `iped_mcp.errors`).
        """
        return self._get(f"bookmarks/{quote(bookmark)}")["data"]

    def create_bookmark(self, name: str) -> None:
        """Cria um bookmark vazio (POST, body vazio). Não erroa se já existe.

        Args:
            name: nome do bookmark.

        Raises:
            IpedError: falha de rede/HTTP (taxonomia `iped_mcp.errors`).
        """
        self._request("POST", f"bookmarks/{quote(name)}")

    def add_to_bookmark(self, bookmark: str, docs: list[dict[str, Any]]) -> None:
        """Adiciona docs ao bookmark. docs: [{"source": str, "id": int}].

        Doc inexistente é ignorado silenciosamente pelo servidor.

        Args:
            bookmark: nome do bookmark.
            docs: pares {source, id} a adicionar.

        Raises:
            IpedError: falha de rede/HTTP (taxonomia `iped_mcp.errors`).
        """
        self._request("PUT", f"bookmarks/{quote(bookmark)}/add", json=docs)

    def remove_from_bookmark(self, bookmark: str, docs: list[dict[str, Any]]) -> None:
        """Remove docs do bookmark. docs: [{"source": str, "id": int}].

        Args:
            bookmark: nome do bookmark.
            docs: pares {source, id} a remover.

        Raises:
            IpedError: falha de rede/HTTP (taxonomia `iped_mcp.errors`).
        """
        self._request("PUT", f"bookmarks/{quote(bookmark)}/remove", json=docs)

    def rename_bookmark(self, old_name: str, new_name: str) -> None:
        """Renomeia um bookmark.

        Args:
            old_name: nome atual.
            new_name: nome novo.

        Raises:
            IpedError: falha de rede/HTTP (taxonomia `iped_mcp.errors`).
        """
        self._request(
            "PUT", f"bookmarks/{quote(old_name)}/rename/{quote(new_name)}"
        )

    def delete_bookmark(self, name: str) -> None:
        """Exclui um bookmark (não os documentos indexados).

        Args:
            name: nome do bookmark.

        Raises:
            IpedError: falha de rede/HTTP (taxonomia `iped_mcp.errors`).
        """
        self._request("DELETE", f"bookmarks/{quote(name)}")

    def get_document_content(
        self, source_id: str, doc_id: int
    ) -> tuple[str, requests.Response]:
        """→ (filename, response); filename do header Content-Disposition.

        O download faz streaming via response.iter_content(). Body vazio
        (Content-Length: 0) = doc sem arquivo.

        Args:
            source_id: source do doc.
            doc_id: id do doc.

        Returns:
            (filename, resposta em streaming).

        Raises:
            IpedError: falha de rede/HTTP antes do streaming começar.
        """
        resp = self._request(
            "GET",
            f"sources/{source_id}/docs/{doc_id}/content",
            timeout=self._stream_timeout,
            source_id=source_id,
            doc_id=doc_id,
        )
        filename = ""
        cd = resp.headers.get("Content-Disposition", "")
        if 'filename="' in cd:
            filename = cd.split('filename="', 1)[1].split('"', 1)[0]
        return filename, resp

    def get_document_text(
        self, source_id: str, doc_id: int, max_bytes: int = _TEXT_READ_CAP
    ) -> tuple[str, int, bool]:
        """→ (text, total_chars, capped). Texto extraído do doc (endpoint /text).

        O stream é chunked (sem Content-Length): lê até max_bytes para contar o
        total. capped=True → total_chars é piso (o texto real é maior).

        Args:
            source_id: source do doc.
            doc_id: id do doc.
            max_bytes: teto de leitura em bytes (padrão 10 MB).

        Returns:
            (texto, nº de caracteres lidos, texto truncado no teto).

        Raises:
            IpedError: falha de rede/HTTP (500 de doc inexistente incluído).
        """
        resp = self._request(
            "GET",
            f"sources/{source_id}/docs/{doc_id}/text",
            timeout=self._stream_timeout,
            source_id=source_id,
            doc_id=doc_id,
        )
        chunks: list[bytes] = []
        total = 0
        capped = False
        for chunk in resp.iter_content(chunk_size=_TEXT_CHUNK_SIZE):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                capped = True
                break
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return text, len(text), capped

    def get_document_thumb(self, source_id: str, doc_id: int) -> requests.Response:
        """→ Response bruto da thumbnail (JPEG). Body vazio = doc sem imagem.

        Args:
            source_id: source do doc.
            doc_id: id do doc.

        Returns:
            Resposta com os bytes da thumbnail.

        Raises:
            IpedError: falha de rede/HTTP (taxonomia `iped_mcp.errors`).
        """
        return self._request(
            "GET",
            f"sources/{source_id}/docs/{doc_id}/thumb",
            timeout=self._stream_timeout,
            source_id=source_id,
            doc_id=doc_id,
        )
