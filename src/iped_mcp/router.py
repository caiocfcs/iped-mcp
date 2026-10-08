"""Proxy/roteador multi-endpoint para o IPED.

O MCP aceita N endpoints IPED no `.env` e age como um roteador transparente:
para o agente continua parecendo um único IPED com um conjunto unificado de
sources. O endpoint nunca é exposto ao agente — erros e rotas são centrados
em source.

Configuração (`.env`):
- `IPED_ENDPOINTS=ep1=http://host:1234,ep2=http://host:1235` (múltiplos);
- `IPED_URL=http://host:1234` (único, id `default`);
- as duas juntas → `RouterStartupError`;
- nenhuma → `http://localhost:1234` (comportamento original).

Regra de ouro: source id é globalmente único. Colisão entre endpoints no
startup → `RouterStartupError` (MCP não sobe). Colisão nova em runtime
(endpoint volta online) → mantém o primeiro, avisa no log.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import requests

from iped_mcp.client import IPEDClient

logger = logging.getLogger("iped_mcp.router")

DEFAULT_IPED_URL = "http://localhost:1234"
_PROBE_TIMEOUT = 5.0
# TTL do cache de listagem de bookmarks (por endpoint). bookmark_exists é
# chamado em add/create/save (1 GET /bookmarks cada); o cache corta a
# repetição. Mutações via MCP invalidam; mudança externa fica ≤ TTL stale.
_BOOKMARKS_TTL = 30.0


class RouterStartupError(Exception):
    """Configuração inválida ou colisão de source id: o MCP não pode subir."""


def _valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def parse_endpoints(
    endpoints_raw: str | None = None, url_raw: str | None = None
) -> dict[str, str]:
    """Lê `IPED_ENDPOINTS`/`IPED_URL` do ambiente → {endpoint_id: url}.

    - `IPED_ENDPOINTS=id1=url1,id2=url2` → múltiplos endpoints (ids únicos,
      URLs válidas);
    - `IPED_URL` → {"default": url};
    - nenhuma → {"default": "http://localhost:1234"};
    - as duas juntas → `RouterStartupError`.
    """
    if endpoints_raw is None:
        endpoints_raw = os.environ.get("IPED_ENDPOINTS", "")
    if url_raw is None:
        url_raw = os.environ.get("IPED_URL", "")
    endpoints_raw = endpoints_raw.strip()
    url_raw = url_raw.strip()

    if endpoints_raw and url_raw:
        raise RouterStartupError(
            "IPED_ENDPOINTS e IPED_URL estão definidas juntas; use apenas uma."
        )

    if endpoints_raw:
        endpoints: dict[str, str] = {}
        for pair in endpoints_raw.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                raise RouterStartupError(
                    f"Endpoint inválido '{pair}' em IPED_ENDPOINTS; "
                    "formato esperado: id=url."
                )
            endpoint_id, url = (part.strip() for part in pair.split("=", 1))
            if not endpoint_id:
                raise RouterStartupError(
                    f"Endpoint inválido '{pair}' em IPED_ENDPOINTS: id vazio."
                )
            if endpoint_id in endpoints:
                raise RouterStartupError(
                    f"Id de endpoint duplicado em IPED_ENDPOINTS: '{endpoint_id}'."
                )
            if not _valid_url(url):
                raise RouterStartupError(
                    f"URL inválida '{url}' para o endpoint '{endpoint_id}'."
                )
            endpoints[endpoint_id] = url
        if not endpoints:
            raise RouterStartupError("IPED_ENDPOINTS está vazia.")
        return endpoints

    if url_raw:
        if not _valid_url(url_raw):
            raise RouterStartupError(f"IPED_URL inválida: '{url_raw}'.")
        return {"default": url_raw}

    return {"default": DEFAULT_IPED_URL}


def _source_id(source: Any) -> str:
    """Extrai o id de um source (dict {id, path} ou string)."""
    if isinstance(source, dict):
        value = source.get("id") or source.get("name") or source
    else:
        value = source
    return str(value)


class Router:
    """Embrulha N `IPEDClient` e roteia por source_id (mesma API do client).

    - Registry em memória `source_id → endpoint_id` (O(1), zero requests
      extras nas rotas com source_id).
    - Startup: sonda `GET /sources/` em paralelo em cada endpoint; colisão de
      source id → `RouterStartupError`; todos offline → aviso (MCP sobe).
    - Source desconhecido em runtime → refresh (re-lista os online) e retry.
    - Toda busca exige source_id e é roteada para o endpoint correspondente.
    - Bookmarks são POR ENDPOINT: toda operação recebe source_id e executa
      apenas no endpoint correspondente.
    """

    def __init__(
        self,
        endpoints: dict[str, str],
        probe_timeout: float = _PROBE_TIMEOUT,
        clients: dict[str, IPEDClient] | None = None,
    ) -> None:
        self._endpoints = dict(endpoints)
        if clients is None:
            clients = {eid: IPEDClient(url) for eid, url in endpoints.items()}
        self._clients = clients
        self._probe_timeout = probe_timeout
        self._registry: dict[str, str] = {}  # source_id → endpoint_id
        self._online: set[str] = set()
        self._bookmarks_cache: dict[int, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()
        self._probe()

    @property
    def base_url(self) -> str:
        """base_url descritiva (log/debug; erros de rede são centrados em source)."""
        return ", ".join(self._endpoints.values())

    # --- sonda / registry ---------------------------------------------------

    def _list_all(
        self, timeout: float | None
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        """`GET /sources/` em paralelo em todos os endpoints.

        → (online {endpoint_id: [sources]}, offline [endpoint_id]).
        """

        def _one(eid: str) -> tuple[str, list[dict[str, Any]] | None]:
            try:
                return eid, self._clients[eid].list_sources(timeout=timeout)
            except requests.RequestException:
                return eid, None

        online: dict[str, list[dict[str, Any]]] = {}
        offline: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, len(self._clients))) as pool:
            for eid, sources in pool.map(_one, list(self._clients)):
                if sources is None:
                    offline.append(eid)
                else:
                    online[eid] = sources
        return online, offline

    def _probe(self) -> None:
        """Sonda todos os endpoints no startup e monta o registry.

        Colisão de source id entre endpoints online → `RouterStartupError`
        (MCP não sobe). Todos offline → aviso em destaque (MCP sobe).
        """
        online, offline = self._list_all(timeout=self._probe_timeout)
        for eid in offline:
            logger.warning(
                "Endpoint IPED '%s' não respondeu no startup (GET /sources/); "
                "seus sources ficam indisponíveis até ele voltar.",
                eid,
            )
        if not online:
            logger.warning(
                "TODOS os endpoints IPED estão offline no startup. O MCP sobe, "
                "mas as tools falham até pelo menos um endpoint responder."
            )
        with self._lock:
            self._online = set(online)
            registry: dict[str, str] = {}
            collisions: dict[str, list[str]] = {}
            for eid, sources in online.items():
                for source in sources:
                    sid = _source_id(source)
                    if sid in registry:
                        collisions.setdefault(sid, [registry[sid]]).append(eid)
                    else:
                        registry[sid] = eid
            if collisions:
                details = "; ".join(
                    f"source '{sid}' está nos endpoints {', '.join(eids)}"
                    for sid, eids in sorted(collisions.items())
                )
                raise RouterStartupError(
                    "Colisão de source id entre endpoints IPED: " + details +
                    ". Source id deve ser globalmente único; corrija a "
                    "configuração dos servidores."
                )
            self._registry = registry
        logger.info(
            "Router IPED pronto: %d endpoint(s), %d source(s) registrados (%s).",
            len(self._endpoints),
            len(self._registry),
            self.base_url,
        )

    def refresh(self) -> None:
        """Re-lista todos os endpoints em paralelo e atualiza registry/online.

        Colisão nova em runtime → mantém o primeiro, avisa no log.
        """
        self.list_sources()

    def online_count(self) -> int:
        """Nº de endpoints online na última sonda/listagem."""
        with self._lock:
            return len(self._online)

    def resolve(self, source_id: str) -> IPEDClient:
        """source_id → `IPEDClient` do endpoint correspondente.

        Source desconhecido → refresh (re-lista os online) e retry; ainda
        assim desconhecido → ValueError.
        """
        with self._lock:
            eid = self._registry.get(source_id)
        if eid is None:
            self.refresh()
            with self._lock:
                eid = self._registry.get(source_id)
        if eid is None:
            raise ValueError(
                f"Source '{source_id}' não encontrado em nenhum endpoint IPED. "
                "Confira o nome com iped_list_sources."
            )
        return self._clients[eid]

    def _call(self, sid: str, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Rota o método para o endpoint de sid; erro de rede → ValueError
        centrado em source (o agente nunca vê endpoint)."""
        client = self.resolve(sid)
        try:
            return getattr(client, method_name)(*args, **kwargs)
        except requests.ConnectionError as exc:
            raise ValueError(
                f"Source '{sid}' indisponível: o servidor correspondente "
                "não responde."
            ) from exc
        except requests.Timeout as exc:
            raise ValueError(
                f"Timeout no source '{sid}': o servidor demorou demais "
                "para responder. Tente novamente ou refine a query."
            ) from exc

    # --- API roteada (mesma superfície do IPEDClient) ------------------------

    def list_sources(self) -> list[dict[str, Any]]:
        """Mescla os sources de todos os endpoints online (sem tag de endpoint).

        Atualiza o registry (sources novos; colisão → mantém o primeiro,
        avisa). Endpoints offline: seus sources simplesmente não aparecem.
        """
        online, offline = self._list_all(timeout=None)
        for eid in offline:
            logger.warning(
                "Endpoint IPED '%s' não respondeu; seus sources não estão na lista.",
                eid,
            )
        with self._lock:
            self._online = set(online)
            merged: list[dict[str, Any]] = []
            registry: dict[str, str] = {}
            for eid, sources in online.items():
                for source in sources:
                    sid = _source_id(source)
                    if sid in registry:
                        logger.warning(
                            "Source '%s' também existe no endpoint '%s'; "
                            "mantendo o primeiro ('%s').",
                            sid,
                            eid,
                            registry[sid],
                        )
                        continue
                    registry[sid] = eid
                    merged.append(source)
            self._registry = registry
        return merged

    def search(
        self,
        query: str,
        source_id: str,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Busca Lucene. source_id obrigatório → roteia p/ o endpoint
        correspondente."""
        return self._call(
            source_id, "search", query, source_id=source_id, timeout=timeout
        )

    def get_document(self, source_id: str, doc_id: int) -> dict[str, Any]:
        return self._call(source_id, "get_document", source_id, doc_id)

    def get_document_content(
        self, source_id: str, doc_id: int
    ) -> tuple[str, requests.Response]:
        return self._call(source_id, "get_document_content", source_id, doc_id)

    def get_document_text(
        self, source_id: str, doc_id: int
    ) -> tuple[str, int, bool]:
        return self._call(source_id, "get_document_text", source_id, doc_id)

    def get_document_thumb(self, source_id: str, doc_id: int) -> requests.Response:
        return self._call(source_id, "get_document_thumb", source_id, doc_id)

    # --- Bookmarks (POR ENDPOINT; roteados por source_id) --------------------

    def _invalidate_bookmarks(self, source_id: str) -> None:
        """Descarta o cache de listagem do endpoint de source_id (pós-mutação)."""
        try:
            client = self.resolve(source_id)
        except ValueError:
            return
        with self._lock:
            self._bookmarks_cache.pop(id(client), None)

    def list_bookmarks(self, source_id: str) -> list[str]:
        client = self.resolve(source_id)
        key = id(client)
        now = time.monotonic()
        cached = self._bookmarks_cache.get(key)
        if cached is not None and now - cached[0] < _BOOKMARKS_TTL:
            return cached[1]
        names = self._call(source_id, "list_bookmarks")
        with self._lock:
            self._bookmarks_cache[key] = (now, names)
        return names

    def get_bookmark_docs(self, bookmark: str, source_id: str) -> list[dict[str, Any]]:
        return self._call(source_id, "get_bookmark_docs", bookmark)

    def create_bookmark(self, name: str, source_id: str) -> None:
        self._call(source_id, "create_bookmark", name)
        self._invalidate_bookmarks(source_id)

    def _validate_docs_endpoint(
        self, docs: list[dict[str, Any]], source_id: str
    ) -> None:
        """Todos os docs devem estar no mesmo endpoint do source_id."""
        target = self.resolve(source_id)
        for doc in docs:
            sid = doc.get("source")
            if not sid:
                continue
            try:
                client = self.resolve(str(sid))
            except ValueError:
                raise ValueError(
                    f"Source do doc '{sid}' não encontrado em nenhum endpoint IPED."
                ) from None
            if client is not target:
                raise ValueError(
                    f"Source do doc '{sid}' está em outro servidor que o source "
                    f"'{source_id}'; bookmarks são por servidor. Use apenas docs "
                    "do mesmo servidor."
                )

    def add_to_bookmark(
        self, bookmark: str, docs: list[dict[str, Any]], source_id: str
    ) -> None:
        self._validate_docs_endpoint(docs, source_id)
        self._call(source_id, "add_to_bookmark", bookmark, docs)
        self._invalidate_bookmarks(source_id)

    def remove_from_bookmark(
        self, bookmark: str, docs: list[dict[str, Any]], source_id: str
    ) -> None:
        self._validate_docs_endpoint(docs, source_id)
        self._call(source_id, "remove_from_bookmark", bookmark, docs)
        self._invalidate_bookmarks(source_id)

    def rename_bookmark(self, old_name: str, new_name: str, source_id: str) -> None:
        self._call(source_id, "rename_bookmark", old_name, new_name)
        self._invalidate_bookmarks(source_id)

    def delete_bookmark(self, name: str, source_id: str) -> None:
        self._call(source_id, "delete_bookmark", name)
        self._invalidate_bookmarks(source_id)


_router: Router | None = None
_router_lock = threading.Lock()


def get_router() -> Router:
    """Singleton do Router (cria e sonda os endpoints na primeira chamada)."""
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = Router(parse_endpoints())
    return _router
