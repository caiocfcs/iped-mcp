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
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from iped_mcp import config
from iped_mcp.client import IPEDClient, _doc_path
from iped_mcp.errors import (
    IpedConnectionError,
    IpedError,
    IpedTimeoutError,
    ResourceNotFoundError,
    no_endpoint_online_message,
    source_not_found_message,
    source_unavailable_message,
    timeout_message,
)

logger = logging.getLogger("iped_mcp.router")

DEFAULT_IPED_URL = "http://localhost:1234"
_PROBE_TIMEOUT = 5.0
# TTL do cache de listagem de bookmarks (por endpoint). bookmark_exists é
# chamado em add/create/save (1 GET /bookmarks cada); o cache corta a
# repetição. Mutações via MCP invalidam; mudança externa fica ≤ TTL stale.
_BOOKMARKS_TTL = 30.0
# Métodos cujo path carrega doc_id: o erro deles fala de "doc", não de source.
_DOC_METHODS = frozenset(
    {
        "get_document",
        "get_document_content",
        "get_document_text",
        "get_document_thumb",
    }
)
# Máximo de caracteres do motivo de falha de endpoint reportado na listagem (AC3).
_FAILURE_REASON_LIMIT = 200

# --- sondas de capacidade do jar (DESIGN 11 / M-11) -------------------------
# Os probes rodam UMA vez no startup, só em endpoints online, e NUNCA
# quebram o servidor: sonda que falha == capacidade desligada (AC2).

# Termo SEM hit esperado na sonda de paginação: força resposta mínima em
# qualquer índice real (o probe observa o envelope da resposta, não os hits).
_PROBE_TERM = "ipedmcp-capability-probe"
# Campo inofensivo pedido na sonda de /properties?fields= (na allowlist F-1).
_PROBE_FIELD = "hash"
# Page-size da sonda: 1 id no máximo (o probe quer o envelope, não o conteúdo).
_PROBE_LIMIT = "1"
# Doc tentado na sonda de fields quando a paginação NÃO foi detectada: sem
# limit funcional não há como achar um doc real barato, e 404/500 de doc
# inexistente já respondem "sem capacidade" na jar atual (2d 19).
_PROBE_FALLBACK_DOC_ID = 1
# Nomes válidos de capacidade (sem magic string nos call sites de supports).
CAPABILITY_SEARCH_PAGINATION = "search_pagination"
CAPABILITY_CASE_SUMMARY = "case_summary"
CAPABILITY_PROPERTIES_FIELDS = "properties_fields"
_CAPABILITY_NAMES = frozenset(
    {
        CAPABILITY_SEARCH_PAGINATION,
        CAPABILITY_CASE_SUMMARY,
        CAPABILITY_PROPERTIES_FIELDS,
    }
)


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


def _failure_reason(exc: BaseException) -> str:
    """Motivo legível (truncado) da falha de um endpoint para listagem."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:_FAILURE_REASON_LIMIT]


def _source_id(source: Any) -> str:
    """Extrai o id de um source (dict {id, path} ou string)."""
    if isinstance(source, dict):
        value = source.get("id") or source.get("name") or source
    else:
        value = source
    return str(value)


@dataclass(frozen=True)
class JarCapabilities:
    """Capacidades observadas do jar de um endpoint (M-11; imutável pós-startup).

    False significa "não detectada" — cobre tanto "o jar não tem o recurso"
    quanto "a sonda falhou" (AC2: falha de sonda nunca crasha e equivale a
    capacidade desligada). O agente vê os valores pela tool iped_list_sources
    (dado, não exceção); nenhum parâmetro de fork segue para a rede sem
    `Router.supports` (env IPED_MCP_FORK_FEATURES=1 E detecção; NR-3/R9).

    Attributes:
        search_pagination: /search ecoa o campo "total" quando limit/offset
            vão explicitamente na URL (paginação real; P1-1 do fork).
        case_summary: GET /case-summary responde 200 (F-5 do fork); a jar
            atual devolve 404 (2d 19).
        properties_fields: /properties?fields= devolve SOMENTE o campo
            pedido (F-1 do fork); 200 "sem efeito" (props completas) conta
            como ausente — é exatamente a armadilha P2-8 de falso positivo.
    """

    search_pagination: bool = False
    case_summary: bool = False
    properties_fields: bool = False

    def as_dict(self) -> dict[str, bool]:
        """Capacidades como dict JSON-seguro (bloco `jar_capabilities` da tool)."""
        return {
            CAPABILITY_SEARCH_PAGINATION: self.search_pagination,
            CAPABILITY_CASE_SUMMARY: self.case_summary,
            CAPABILITY_PROPERTIES_FIELDS: self.properties_fields,
        }


def _probe_pagination(client: IPEDClient, sid: str, timeout: float) -> bool:
    """Tem paginação o jar? GET /search?limit=1 com q SEM hit ecoa "total"?

    Args:
        client: cliente do endpoint sondado.
        sid: source âncora da sonda (primeiro source online do endpoint).
        timeout: segundos da sonda (timeout de startup do Router).

    Returns:
        True quando o corpo da resposta contém "total" (P1-1); False em
        qualquer outro caso, inclusive falha de rede/HTTP (AC2).
    """
    try:
        payload = client._get(  # noqa: SLF001 — mesmo pacote: o search()
            "search",  # público descarta o envelope, e "total" está nele
            params={"q": _PROBE_TERM, "sourceID": sid, "limit": _PROBE_LIMIT},
            timeout=timeout,
            source_id=sid,
            retry_server_errors=False,
        )
    except IpedError:
        return False
    return isinstance(payload, dict) and "total" in payload


def _probe_case_summary(client: IPEDClient, sid: str, timeout: float) -> bool:
    """Existe /case-summary no jar? GET com sourceID responde 200 JSON (F-5)?

    Args:
        client: cliente do endpoint sondado.
        sid: source âncora da sonda.
        timeout: segundos da sonda.

    Returns:
        True em 200 com JSON não vazio; False em 404 e em qualquer falha.
    """
    try:
        payload = client._get(
            "case-summary",
            params={"sourceID": sid},
            timeout=timeout,
            source_id=sid,
            retry_server_errors=False,
        )
    except IpedError:
        return False
    return isinstance(payload, dict) and bool(payload)


def _first_group_id(payload: object) -> int | None:
    """Primeiro doc id de uma resposta agrupada de /search (None sem hits)."""
    groups = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return None
    for group in groups:
        ids = group.get("ids") if isinstance(group, dict) else None
        if isinstance(ids, list) and ids:
            return int(ids[0])
    return None


def _probe_fields_response_respects(payload: object) -> bool:
    """A resposta de /properties veio restrita ao campo sondado?

    Um jar que IGNORA `fields=` (P2-8) devolve as propriedades completas —
    isso é "sem efeito" e conta como capacidade AUSENTE, nunca como
    detecção (risco de falso positivo apontado no Design 11).

    Args:
        payload: corpo JSON da resposta de /properties?fields=<campo>.

    Returns:
        True quando há ao menos uma chave e todas são o campo sondado.
    """
    if not isinstance(payload, dict):
        return False
    props = payload.get("properties")
    if not isinstance(props, dict):
        props = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    keys = set(props)
    return bool(keys) and keys <= {_PROBE_FIELD}


def _probe_properties_fields(
    client: IPEDClient, sid: str, timeout: float, pagination: bool
) -> bool:
    """Tem F-1 o jar? /properties?fields= ecoa SOMENTE o campo pedido?

    Args:
        client: cliente do endpoint sondado.
        sid: source âncora da sonda.
        timeout: segundos da sonda.
        pagination: paginação já detectada neste endpoint (permite achar um
            doc real barato com limit=1; sem ela usa o doc-id de fallback).

    Returns:
        True em 200 com props filtradas; False em 404, em 200 "sem efeito"
        e em qualquer falha de rede/HTTP.
    """
    doc_id = _PROBE_FALLBACK_DOC_ID
    if pagination:
        try:
            sample = client._get(
                "search",
                params={"q": "*", "sourceID": sid, "limit": _PROBE_LIMIT},
                timeout=timeout,
                source_id=sid,
                retry_server_errors=False,
            )
        except IpedError:
            sample = None
        found = _first_group_id(sample)
        if found is not None:
            doc_id = found
    path = _doc_path(sid, doc_id, "properties")
    try:
        payload = client._get(
            path,
            params={"fields": _PROBE_FIELD},
            timeout=timeout,
            source_id=sid,
            doc_id=doc_id,
            retry_server_errors=False,
        )
    except IpedError:
        return False
    return _probe_fields_response_respects(payload)


def _probe_endpoint_capabilities(
    client: IPEDClient, sid: str, timeout: float
) -> JarCapabilities:
    """Executa as três sondas do endpoint (3–4 chamadas; custo do startup).

    Args:
        client: cliente do endpoint sondado.
        sid: source âncora (primeiro source online do endpoint).
        timeout: segundos por sonda.

    Returns:
        `JarCapabilities` com uma flag por capacidade detectada.
    """
    pagination = _probe_pagination(client, sid, timeout)
    return JarCapabilities(
        search_pagination=pagination,
        case_summary=_probe_case_summary(client, sid, timeout),
        properties_fields=_probe_properties_fields(client, sid, timeout, pagination),
    )


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
        self._capabilities: dict[str, JarCapabilities] = {}
        self._lock = threading.Lock()
        self._probe()

    @property
    def base_url(self) -> str:
        """base_url descritiva (log/debug; erros de rede são centrados em source)."""
        return ", ".join(self._endpoints.values())

    # --- sonda / registry ---------------------------------------------------

    def _list_all(
        self, timeout: float | None
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str], dict[str, str]]:
        """`GET /sources/` em paralelo em todos os endpoints.

        → (online {endpoint_id: [sources]}, offline [endpoint_id],
           {endpoint_id: motivo da falha}).
        """

        def _one(
            eid: str,
        ) -> tuple[str, list[dict[str, Any]] | None, str | None]:
            try:
                return eid, self._clients[eid].list_sources(timeout=timeout), None
            except (IpedError, requests.RequestException) as exc:
                return eid, None, _failure_reason(exc)

        online: dict[str, list[dict[str, Any]]] = {}
        offline: list[str] = []
        failures: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(self._clients))) as pool:
            for eid, sources, reason in pool.map(_one, list(self._clients)):
                if sources is None:
                    offline.append(eid)
                    failures[eid] = reason or "falha desconhecida"
                else:
                    online[eid] = sources
        return online, offline, failures

    def _probe(self) -> None:
        """Sonda todos os endpoints no startup e monta o registry.

        Colisão de source id entre endpoints online → `RouterStartupError`
        (MCP não sobe). Todos offline → aviso em destaque (MCP sobe).
        """
        online, offline, failures = self._list_all(timeout=self._probe_timeout)
        for eid in offline:
            logger.warning(
                "Endpoint IPED '%s' não respondeu no startup (GET /sources/): "
                "%s — seus sources ficam indisponíveis até ele voltar.",
                eid,
                failures.get(eid, "sem motivo"),
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
        self._probe_capabilities(online)
        logger.info(
            "Router IPED pronto: %d endpoint(s), %d source(s) registrados (%s).",
            len(self._endpoints),
            len(self._registry),
            self.base_url,
        )

    def _probe_capabilities(self, online: dict[str, list[dict[str, Any]]]) -> None:
        """Sonda capacidades do jar UMA vez por endpoint online no startup (M-11).

        As sondas rodam em paralelo e NENHUMA falha derruba o servidor:
        capacidade indetectada fica False e o motivo vai para o log (AC2).
        Endpoints offline não são sondados (já aparecem como dado em
        `list_sources_status`); endpoint que só ficar online em runtime
        mantém capacidades desligadas até o próximo startup (regra
        "detectar uma vez" do design).

        Args:
            online: {endpoint_id: [sources]} da sonda de startup.
        """

        def _one(eid: str) -> tuple[str, JarCapabilities]:
            sources = online.get(eid) or []
            if not sources:
                return eid, JarCapabilities()
            try:
                caps = _probe_endpoint_capabilities(
                    self._clients[eid], _source_id(sources[0]), self._probe_timeout
                )
                return eid, caps
            except Exception as exc:  # noqa: BLE001 — contrato nunca-crash (AC2):
                # sonda inesperada falhou == capacidades desligadas, com o
                # motivo no log (falha esperada vira dado de diagnóstico).
                logger.warning(
                    "Sonda de capacidades do endpoint IPED '%s' falhou (%s); "
                    "capacidades desse endpoint ficam desligadas.",
                    eid,
                    _failure_reason(exc),
                )
                return eid, JarCapabilities()

        caps_map: dict[str, JarCapabilities] = {}
        if online:
            with ThreadPoolExecutor(max_workers=max(1, len(online))) as pool:
                for eid, caps in pool.map(_one, list(online)):
                    caps_map[eid] = caps
        with self._lock:
            self._capabilities = caps_map
        logger.info(
            "Capacidades do jar IPED detectadas: %s",
            {eid: caps.as_dict() for eid, caps in sorted(caps_map.items())},
        )

    def capabilities(self) -> dict[str, JarCapabilities]:
        """Snapshot {endpoint_id: JarCapabilities} detectado no startup (M-11).

        Returns:
            Cópia do mapa; os valores são dataclasses congeladas.
        """
        with self._lock:
            return dict(self._capabilities)

    def capabilities_payload(self) -> dict[str, Any]:
        """Capacidades detectadas + flag de env da superfície do fork (JSON-safe).

        Alimenta a tool iped_list_sources: o agente consumidor vê com que
        jar está falando ANTES de pedir qualquer recurso (dado, não
        exceção). Endpoints offline não têm entrada (a falta já é reportada
        em `offline_endpoints`).

        Returns:
            {"fork_features_enabled": bool,
             "endpoints": [{endpoint, url, search_pagination, case_summary,
                            properties_fields}, ...]}.
        """
        with self._lock:
            items = [
                {
                    "endpoint": eid,
                    "url": self._endpoints.get(eid, ""),
                    **caps.as_dict(),
                }
                for eid, caps in sorted(self._capabilities.items())
            ]
        return {
            "fork_features_enabled": config.FORK_FEATURES_ENABLED,
            "endpoints": items,
        }

    def supports(self, capability: str, source_id: str) -> bool:
        """Gate da superfície do fork: capacidade detectada E env ligado (M-11).

        Parâmetros de fork (limit/offset explícitos, fields=, filtros F-2)
        só podem seguir para a rede quando este método responde True; caso
        contrário a tool deve recusar com ValueError nomeando a capacidade
        (AC4 — nunca ignorar em silêncio, o contrário de P2-8). Sem o env
        IPED_MCP_FORK_FEATURES=1 nada é enviado jamais (NR-3/R9).

        Args:
            capability: nome da capacidade ("search_pagination",
                "case_summary" ou "properties_fields").
            source_id: source cujo endpoint será consultado.

        Returns:
            True quando a capacidade foi detectada no endpoint do source
            E a superfície do fork está ligada por env.

        Raises:
            ValueError: nome de capacidade desconhecido.
        """
        if capability not in _CAPABILITY_NAMES:
            raise ValueError(
                f"Capacidade desconhecida '{capability}'; "
                f"válidas: {sorted(_CAPABILITY_NAMES)}."
            )
        with self._lock:
            eid = self._registry.get(source_id)
            caps = self._capabilities.get(eid) if eid is not None else None
        return bool(
            caps is not None
            and config.FORK_FEATURES_ENABLED
            and getattr(caps, capability)
        )

    def refresh(self) -> None:
        """Re-lista todos os endpoints em paralelo e atualiza registry/online.

        Colisão nova em runtime → mantém o primeiro, avisa no log.
        """
        self.list_sources_status()

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
            if self.online_count() == 0:
                # Servidor mudo ≠ source inexistente (AC2): a re-listagem não
                # viu NENHUM source — é indisponibilidade, não ausência.
                raise ValueError(no_endpoint_online_message(source_id))
            raise ValueError(source_not_found_message(source_id))
        return self._clients[eid]

    def _call(self, sid: str, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Rota o método para o endpoint de sid; erro IPED → ValueError com
        texto estável centrado em source (o agente nunca vê endpoint nem
        exceção de transporte).

        Sem exceções tipadas vazando (M-14): o caso especial que devolvia
        `ServerError` de `search` foi revogado — a política de retry do M-3
        já decide por classe (5xx de busca nunca é re-tentado no client),
        então todo erro IPED segue o fluxo padrão M-2 acima.
        """
        client = self.resolve(sid)
        try:
            return getattr(client, method_name)(*args, **kwargs)
        except IpedTimeoutError as exc:
            raise ValueError(timeout_message(sid)) from exc
        except IpedConnectionError as exc:
            raise ValueError(source_unavailable_message(sid)) from exc
        except ResourceNotFoundError as exc:
            if method_name in _DOC_METHODS:
                raise ValueError(str(exc)) from exc
            raise ValueError(source_not_found_message(sid)) from exc
        except IpedError as exc:
            raise ValueError(str(exc)) from exc

    # --- API roteada (mesma superfície do IPEDClient) ------------------------

    def list_sources_status(self) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Mescla os sources dos endpoints online e reporta falha por endpoint.

        A falha de um servidor é DEVOLVIDA como dado ({endpoint: motivo}),
        nunca engolida (M-2/AC3): lista vazia com todos offline é
        diagnóstico, não "caso sem fontes". Atualiza o registry (sources
        novos; colisão → mantém o primeiro, avisa).

        Returns:
            (sources mesclados sem tag de endpoint,
             {endpoint_id: motivo da falha} — vazio quando todos respondem).
        """
        online, offline, failures = self._list_all(timeout=None)
        for eid in offline:
            logger.warning(
                "Endpoint IPED '%s' não respondeu (%s); seus sources não estão na lista.",
                eid,
                failures.get(eid, "sem motivo"),
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
        return merged, failures

    def list_sources(self) -> list[dict[str, Any]]:
        """Sources mesclados de todos os endpoints online (sem tag de endpoint).

        Endpoints offline: seus sources não aparecem; o motivo de cada falha
        fica em `list_sources_status` (dado, não aviso silencioso).
        """
        merged, _ = self.list_sources_status()
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
