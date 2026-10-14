"""Fixtures de contrato da WebAPI: servidor HTTP fake (stdlib) + cliente.

O fake reproduz os vereditos de runtime da FASE 2d (research/reports/
phase-2d-runtime-webapi.md, build custom do operador, 2026-10-11):

- envelope ``{"data": [...]}`` em /sources/, /search, /bookmarks[/nome];
- doc no TOPO (sem envelope) em /sources/{sid}/docs/{id}, propriedades
  com valores em LISTA;
- doc inexistente -> HTTP 500 (HTML Grizzly); source inexistente -> 404
  text/plain "source not found: <id>";
- /search SEM paginação: sem campo "total", limit/offset ignorados;
- /text em stream chunked; /thumb JPEG; thumb com body vazio = sem imagem;
- Content-Disposition ``filename="..."`` no /content (e variante ausente);
- GET /bookmarks/{inexistente} -> 200 {"data": []} (PB2); mutações
  (POST/PUT/DELETE) -> 200 com body vazio.

Nada aqui exige servidor externo: o fake sobe em 127.0.0.1 numa porta
efêmera, num thread daemon (AC6 do Design 01 — suíte 100% offline).
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from iped_mcp.client import IPEDClient

# --- Constantes sinteticas do fake (original medido na FASE 2d foi
# --- sanitizado na fase 11 — ver research/reports/phase-11-sanitization.md) ---
SOURCE_ID = "pessoa_teste_13@example.invalid"
SOURCE_PATH = "E:\\TESTE\\Nuvem GOOGLE_TESTE"
SOURCE_ID_2 = "pessoa_teste_4@example.invalid-0000001"
UNKNOWN_SOURCE = "naoexiste9999zz"
DOC_NO_IMAGE_ID = 29  # sem thumb (2d 13b); md5 conhecido (2d 3c) e Regex:EMAIL
DOC_TEXT_ID = 53  # arquivo + texto + thumbnail (2d #11-#13)
DOC_NO_DISPOSITION_ID = 7  # variante sem Content-Disposition (Design 01 AC3)
MISSING_DOC_ID = 999999999  # -> 500 HTML Grizzly (2d 10)
DOC_MD5_UPPER = "2911F8019EA1915573D42899EE32A90C"  # valor MAIUSCULO no jar
DOC_MD5_TEXT = "0A0B0C0D0E0F10111213141516171819"
EXISTING_BOOKMARK = "projeto-x"
BOOKMARK_NAME_WITH_SPACE = "meu bookmark"
CONTENT_FILENAME = "AdobePhotoshopExpress_foto.jpg"
JPEG_MAGIC = b"\xff\xd8\xff\xe0"
JPEG_BODY = JPEG_MAGIC + b"\x00" * 200
TEXT_BODY = "texto extraido TIKA ".encode("utf-8") * 500
GRIZZLY_500 = (
    b"<html><head><title>Grizzly Framework 2.3.31</title></head>"
    b"<body><h2>HTTP Status 500 - Internal Server Error</h2></body></html>"
)
NO_HITS_QUERY = "zzznaoexiste"
ALL_DOC_IDS = [DOC_NO_IMAGE_ID, DOC_TEXT_ID, DOC_NO_DISPOSITION_ID]


def make_doc(doc_id: int, name: str, props: dict[str, list[Any]]) -> dict[str, Any]:
    """Monta um doc no formato exato do jar (2d #3): doc no topo, props em lista.

    Args:
        doc_id: Id numerico do documento.
        name: Nome do arquivo (propriedade ``name``).
        props: Propriedades; cada valor e sempre uma LISTA (WI-1/P12).

    Returns:
        Dicionario no envelope do jar (sem "data": o doc vem no topo).
    """
    return {
        "bookmarks": [],
        "id": doc_id,
        "luceneId": f"{SOURCE_ID}@{doc_id}",
        "properties": {"name": [name], **props},
        "selected": False,
        "source": SOURCE_ID,
    }


def default_docs() -> dict[tuple[str, int], dict[str, Any]]:
    """Catálogo de docs do fake espelhando os probes da 2d."""
    docs = {
        (SOURCE_ID, DOC_NO_IMAGE_ID): make_doc(
            DOC_NO_IMAGE_ID,
            "msg_029.ogg",
            {
                "md5": [DOC_MD5_UPPER],
                "ext": ["ogg"],
                "Regex:EMAIL": ["pessoa_teste_13@example.invalid"],
            },
        ),
        (SOURCE_ID, DOC_TEXT_ID): make_doc(
            DOC_TEXT_ID,
            CONTENT_FILENAME,
            {"md5": [DOC_MD5_TEXT], "ext": ["jpg"]},
        ),
        (SOURCE_ID, DOC_NO_DISPOSITION_ID): make_doc(
            DOC_NO_DISPOSITION_ID,
            "sem-disposition.bin",
            {},
        ),
    }
    return docs


@dataclass
class FakeWebAPI:
    """Estado observavel do fake: catalogs de docs/bookmarks + log de pedidos.

    Attributes:
        base_url: URL base preenchida pelo fixture apos bind da porta.
        sources: Lista [{"id", "path"}] como no GET /sources/ real.
        docs: {(source_id, doc_id): documento no formato do jar}.
        bookmarks: {nome: [{"source", "ids"}]} (forma real 2a/2c).
        requests_log: Um dict por pedido recebido (method, path, raw_target,
            query, req_body, status, resp_body) para pin de URL/params.
        categories: catálogo do GET /categories (vazio = sem categorias).
        category_hits: {categoria: [ids}] para queries category:"<cat>".
        has_categories_endpoint: False simula jar SEM o endpoint (404).
        fail_search_queries: queries `q` exatas que respondem 500 (falha
            parcial de censo).
        search_latency_s: latência artificial por /search (medição de
            concorrência do censo); 0 = sem latência (suíte existente).
        search_peak_inflight: pico observado de /search simultâneos.
        content_types: {doc_id: Content-Type} do GET /content (M-10: fixture
            P1-3 — extensão do export deve seguir o type real do jar).
        has_pagination: True = jar FORK com P1-1 (limit/offset respeitados;
            "total" só com paginação explícita); False = jar atual ignora.
        has_case_summary: True = GET /case-summary 200 (F-5 do fork).
        has_properties_fields: "absent"|"noeffect"|"filtered" — estado do
            F-1 no jar sondado (404 / 200 com props completas / 200 filtrado).
    """

    base_url: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    docs: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    bookmarks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    requests_log: list[dict[str, Any]] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    category_hits: dict[str, list[int]] = field(default_factory=dict)
    has_categories_endpoint: bool = True
    fail_search_queries: set[str] = field(default_factory=set)
    search_latency_s: float = 0.0
    search_inflight: int = 0
    search_peak_inflight: int = 0
    content_types: dict[int, str] = field(default_factory=dict)
    has_pagination: bool = False
    has_case_summary: bool = False
    has_properties_fields: str = "absent"
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def source_ids(self) -> set[str]:
        """Conjunto de ids de sources registrados no fake."""
        return {s["id"] for s in self.sources}

    def search_ids(self, query: str) -> list[int]:
        """Resolve a query Lucene como o jar da 2d (sem paginação, sem erro).

        Args:
            query: Valor cru do parametro ``q`` (com escapes ``\\:`` do client).

        Returns:
            Ids casados no SOURCE_ID; lista vazia quando nada casa (campo
            inexistente = 0 hits mudo, WI-16).
        """
        q = query.replace("\\:", ":")
        cat = re.fullmatch(r'category:"([^"]*)"', q)
        if cat:
            return list(self.category_hits.get(cat.group(1), []))
        if q.lower().startswith("md5:"):
            value = q.split(":", 1)[1]
            return [DOC_NO_IMAGE_ID] if value.upper() == DOC_MD5_UPPER else []
        if q.startswith("ufed:MD5"):
            return [DOC_NO_IMAGE_ID]
        if q.startswith("Regex:"):
            return self._regex_hits(q.split(":", 2)[1])
        if q == "*":
            # Match-all reflete o catalogo atual de docs do SOURCE_ID (M-7:
            # testes adicionam docs p/ amostragem; com o catalogo padrao o
            # resultado e identico a ALL_DOC_IDS, mesma ordem).
            return [i for (src, i) in self.docs if src == SOURCE_ID]
        return []

    def _regex_hits(self, name: str) -> list[int]:
        """Ids dos docs do SOURCE_ID com a propriedade `Regex:<name>` (M-9).

        generalize o pino antigo (so EMAIL): o detector casa qualquer doc
        que carregue a propriedade, preservando o resultado do catalogo
        padrao (so DOC_NO_IMAGE_ID tem Regex:EMAIL) e a ordem do catalogo.

        Args:
            name: nome do detector sem o prefixo (ex.: EMAIL, TEST).

        Returns:
            Ids na ordem de insercao do catalogo; [] quando nada carrega a
            propriedade (0 hits mudo, WI-16).
        """
        key = f"Regex:{name}"
        return [
            i
            for (src_, i), d in self.docs.items()
            if src_ == SOURCE_ID and key in d.get("properties", {})
        ]

    def add_bookmark(self, name: str) -> None:
        """Cria bookmark vazio; idempotente (POST 200 mesmo se ja existe)."""
        self.bookmarks.setdefault(name, [])

    def add_docs(self, name: str, docs: list[dict[str, Any]]) -> None:
        """Adiciona docs ao bookmark ignorando docs inexistentes (PB5, 2a)."""
        groups = self.bookmarks.setdefault(name, [])
        for item in docs:
            key = (item.get("source"), item.get("id"))
            if key not in self.docs:
                continue
            group = next((g for g in groups if g["source"] == key[0]), None)
            if group is None:
                group = {"source": key[0], "ids": []}
                groups.append(group)
            if key[1] not in group["ids"]:
                group["ids"].append(key[1])

    def remove_docs(self, name: str, docs: list[dict[str, Any]]) -> None:
        """Remove docs do bookmark (ignora ausentes)."""
        wanted = {(d.get("source"), d.get("id")) for d in docs}
        for group in self.bookmarks.get(name, []):
            group["ids"] = [i for i in group["ids"] if (group["source"], i) not in wanted]


def make_default_api() -> FakeWebAPI:
    """Instancia o fake com sources/docs/bookmarks padrao da 2d."""
    api = FakeWebAPI(
        sources=[
            {"id": SOURCE_ID, "path": SOURCE_PATH},
            {"id": SOURCE_ID_2, "path": "E:\\TESTE\\icloud"},
        ],
        docs=default_docs(),
        bookmarks={EXISTING_BOOKMARK: [{"source": SOURCE_ID, "ids": [DOC_NO_IMAGE_ID]}]},
    )
    return api


class FakeAPIHandler(BaseHTTPRequestHandler):
    """Handler HTTP que implementa os 13 endpoints do client (client.py:51-177).

    Roteia por (metodo, caminho) e responde com os fixtures da 2d. Cada
    pedido e gravado em ``api.requests_log`` para pin de URL/params/corpo.
    """

    protocol_version = "HTTP/1.1"
    api: FakeWebAPI  # injetado pela fabrica por servidor

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silencia o log padrao do http.server (ruido no output do pytest)."""

    # --- infra de resposta -------------------------------------------------

    def _send(self, status: int, ctype: str, body: bytes, headers: dict[str, str] | None = None) -> None:
        """Envia resposta com Content-Length; loga status/body ANTES do write (sem race)."""
        self._finish(status, body)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_chunked(self, ctype: str, body: bytes) -> None:
        """Envia em Transfer-Encoding: chunked (como o /text do jar, 2d #12)."""
        self._finish(200, body)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        step = 4096
        for start in range(0, len(body), step):
            piece = body[start : start + step]
            self.wfile.write(f"{len(piece):X}\r\n".encode() + piece + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _finish(self, status: int, body: bytes) -> None:
        """Marca status/corpo da resposta na entrada de log do pedido atual."""
        entry = getattr(self, "_entry", None)
        if entry is not None:
            entry["status"] = status
            entry["resp_body"] = body

    def _json(self, payload: Any) -> None:
        """Responde 200 application/json com o envelope dado."""
        self._send(200, "application/json", json.dumps(payload).encode("utf-8"))

    def _empty_200(self) -> None:
        """Responde 200 com body vazio (contrato das mutacoes de bookmark)."""
        self._send(200, "text/plain", b"")

    def _grizzly_500(self) -> None:
        """Responde o 500 HTML do Grizzly (doc inexistente/erro, 2d 10/30)."""
        self._send(500, "text/html", GRIZZLY_500)

    def _source_not_found(self, sid: str) -> None:
        """Responde 404 text/plain do P2-1 (source inexistente, 2d 04)."""
        self._send(404, "text/plain", f"source not found: {sid}".encode("utf-8"))

    # --- despacho ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        """Despacha GET (endpoints #1-#5, #11-#13)."""
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        """Despacha POST (#6 create bookmark)."""
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        """Despacha PUT (#7 add, #8 remove, #9 rename)."""
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        """Despacha DELETE (#10 delete bookmark)."""
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        """Registra o pedido e roteia por (metodo, caminho)."""
        split = urlsplit(self.path)
        parts = [unquote(p) for p in split.path.split("/") if p]
        self._entry = {
            "method": method,
            "path": unquote(split.path),
            "raw_target": self.path,
            "query": parse_qs(split.query),
            "req_body": self._read_body(),
        }
        self.api.requests_log.append(self._entry)
        if method == "GET" and parts[:1] == ["sources"]:
            return self._route_doc(method, parts)
        if parts[:1] == ["search"]:
            return self._route_search()
        if parts[:1] == ["bookmarks"]:
            return self._route_bookmarks(method, parts)
        if method == "GET" and parts[:1] == ["categories"]:
            return self._route_categories()
        if method == "GET" and parts[:1] == ["case-summary"]:
            return self._route_case_summary()
        self._send(404, "text/plain", b"not found")

    def _read_body(self) -> bytes:
        """Lê o corpo da requisição pelo Content-Length (0 se ausente)."""
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # --- endpoints ------------------------------------------------------------

    def _route_doc(self, method: str, parts: list[str]) -> None:
        """GET /sources/ (#1) e /sources/{sid}/docs/{id}[/{content|text|thumb}]."""
        if parts == ["sources"]:
            return self._json({"data": self.api.sources})
        sid = parts[1] if len(parts) > 1 else ""
        if method != "GET" or sid not in self.api.source_ids():
            return self._source_not_found(sid)
        if len(parts) < 4 or parts[2] != "docs":
            return self._send(404, "text/plain", b"not found")
        doc = self.api.docs.get((sid, _as_int(parts[3])))
        if doc is None:
            return self._grizzly_500()
        return self._route_doc_subview(parts, doc)

    def _route_doc_subview(self, parts: list[str], doc: dict[str, Any]) -> None:
        """Responde a sub-visao do doc: topo (#3), content, text ou thumb."""
        view = parts[4] if len(parts) > 4 else ""
        doc_id = doc["id"]
        if view == "":
            return self._json(doc)
        if view == "content":
            return self._send_content(doc_id)
        if view == "text":
            return self._send_chunked("text/plain", TEXT_BODY)
        if view == "thumb":
            return self._send_thumb(doc_id)
        if view == "properties":
            return self._route_properties_fields(doc)
        self._send(404, "text/plain", b"not found")

    def _send_content(self, doc_id: int) -> None:
        """GET /content: com Content-Disposition, exceto doc 7 (variante)."""
        headers = {}
        if doc_id != DOC_NO_DISPOSITION_ID:
            headers["Content-Disposition"] = f'attachment; filename="{CONTENT_FILENAME}"'
        ctype = self.api.content_types.get(doc_id, "application/octet-stream")
        self._send(200, ctype, JPEG_BODY, headers)

    def _send_thumb(self, doc_id: int) -> None:
        """GET /thumb: JPEG p/ doc 53; body vazio = sem imagem (2d 13b)."""
        body = JPEG_BODY if doc_id == DOC_TEXT_ID else b""
        self._send(200, "image/jpg", body)

    def _route_categories(self) -> None:
        """GET /categories: {"data": [nomes]} (contrato icaro :194-196).

        Com `has_categories_endpoint=False` responde 404 text/plain — o
        padrao dos endpoints ausentes no jar-alvo (2d: /properties,
        /case-summary) — para pinar o caminho de erro do censo (M-6).
        """
        if not self.api.has_categories_endpoint:
            return self._send(404, "text/plain", b"not found")
        self._json({"data": list(self.api.categories)})

    def _route_case_summary(self) -> None:
        """GET /case-summary: 200 com data no jar FORK; 404 no jar atual (2d 19)."""
        if not self.api.has_case_summary:
            return self._send(404, "text/plain", b"not found")
        self._json({"data": {"totalItems": 0, "categories": []}})

    def _route_properties_fields(self, doc: dict[str, Any]) -> None:
        """GET /docs/{id}/properties?fields=: os 3 estados do F-1 no jar.

        has_properties_fields: "absent" = jar atual (404); "noeffect" = jar
        que responde 200 com as props COMPLETAS e ignora fields= (armadilha
        P2-8 que a deteccao precisa rejeitar); "filtered" = fork F-1 (200
        somente com os campos pedidos na query).
        """
        mode = self.api.has_properties_fields
        if mode == "absent":
            return self._send(404, "text/plain", b"not found")
        props = dict(doc.get("properties", {}))
        if mode == "noeffect":
            return self._json({"properties": props})
        requested = [
            f for f in (self._entry["query"].get("fields", [""])[0]).split(",") if f
        ]
        self._json({"properties": {f: props.get(f, [""]) for f in requested}})

    def _route_search(self) -> None:
        """GET /search (#2): source desconhecido -> 404; senao grupos sem total.

        Instrumentacao do censo (M-6): `fail_search_queries` responde 500
        para as queries exatas listadas; `search_latency_s` latencia
        artificial permite medir o pico de buscas simultaneas.
        """
        api = self.api
        query = self._entry["query"]
        sid = query.get("sourceID", [None])[0]
        if sid is not None and sid not in api.source_ids():
            return self._source_not_found(sid)
        q = query.get("q", [""])[0]
        if q in api.fail_search_queries:
            return self._grizzly_500()
        if api.search_latency_s:
            with api.lock:
                api.search_inflight += 1
                api.search_peak_inflight = max(api.search_peak_inflight, api.search_inflight)
            try:
                time.sleep(api.search_latency_s)
            finally:
                with api.lock:
                    api.search_inflight -= 1
        ids = api.search_ids(q)
        data = [{"source": sid or SOURCE_ID, "ids": ids}] if ids else []
        payload = {"data": data}
        # Jar FORK (P1-1): com has_pagination, limit/offset sao respeitados e
        # "total" so aparece quando a paginacao e explicita; sem o flag o
        # jar atual ignora os parametros (P2-8) e o corpo sem o campo e
        # identico ao do baseline (pin M-1 byte-a-byte preservado).
        if api.has_pagination and ("limit" in query or "offset" in query):
            offset = _as_int(query.get("offset", ["0"])[0])
            limit = _as_int(query.get("limit", [str(len(ids))])[0])
            for group in data:
                group["ids"] = group["ids"][offset:offset + limit]
            payload["total"] = len(ids)
        self._json(payload)

    def _route_bookmarks(self, method: str, parts: list[str]) -> None:
        """Rota dos endpoints #4-#10 (bookmarks)."""
        api = self.api
        if len(parts) == 1 and method == "GET":
            return self._json({"data": sorted(api.bookmarks)})
        if len(parts) == 2:
            return self._bookmark_single(method, parts[1])
        if len(parts) == 3 and parts[2] in ("add", "remove") and method == "PUT":
            return self._bookmark_mutate_docs(parts[1], parts[2])
        if len(parts) == 4 and parts[2] == "rename" and method == "PUT":
            api.bookmarks[parts[3]] = api.bookmarks.pop(parts[1], [])
            return self._empty_200()
        self._send(404, "text/plain", b"not found")

    def _bookmark_single(self, method: str, name: str) -> None:
        """GET/POST/DELETE em /bookmarks/{nome}."""
        if method == "GET":
            return self._json({"data": self.api.bookmarks.get(name, [])})
        if method == "POST":
            self.api.add_bookmark(name)
            return self._empty_200()
        if method == "DELETE":
            self.api.bookmarks.pop(name, None)
            return self._empty_200()
        self._send(404, "text/plain", b"not found")

    def _bookmark_mutate_docs(self, name: str, action: str) -> None:
        """PUT /bookmarks/{nome}/add|remove com corpo [{source,id}]."""
        docs = json.loads(self._entry["req_body"].decode("utf-8") or "[]")
        if action == "add":
            self.api.add_bookmark(name)
            self.api.add_docs(name, docs)
        else:
            self.api.remove_docs(name, docs)
        self._empty_200()


def _as_int(value: str) -> int:
    """Converte segmento de path em int (-1 para nao-numerico => doc ausente)."""
    try:
        return int(value)
    except ValueError:
        return -1


@pytest.fixture()
def webapi() -> Any:
    """Sobe o fake em 127.0.0.1:porta-efemera e devolve o estado observavel."""
    api = make_default_api()
    handler = type("BoundHandler", (FakeAPIHandler,), {"api": api})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield api
    server.shutdown()
    server.server_close()


@pytest.fixture()
def client(webapi: Any) -> Any:
    """IPEDClient apontado para o fake (mesma classe usada em producao)."""
    return IPEDClient(base_url=webapi.base_url)


@pytest.fixture()
def fake_router(webapi: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Router real apontado para o fake, instalado no singleton get_router().

    Tambem zera os caches de fetch.py para evitar contaminacao entre testes.
    """
    from iped_mcp import fetch, router as router_mod
    from iped_mcp.core import census as census_mod

    monkeypatch.setattr(fetch, "_cache", {})
    monkeypatch.setattr(fetch, "_text_cache", {})
    monkeypatch.setattr(census_mod, "_result_cache", {})
    router = router_mod.Router({"default": webapi.base_url})
    # O startup do Router agora sonda capacidades (M-11): os pedidos de
    # sonda nao sao trafego das tools e saem do log para nao poluir os
    # pinos de URL/contagem dos testes existentes.
    webapi.requests_log.clear()
    monkeypatch.setattr(router_mod, "_router", router)
    return router


@pytest.fixture(autouse=True)
def _sem_backoff_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zera o backoff padrão do client para a suíte não dormir de verdade.

    A política continua valendo (tentativas e decisões não mudam); só a
    espera real é substituída. Testes de resiliência injetam `sleeper`/
    `clock` próprios e não são afetados (injeção tem precedência).
    """
    import time as real_time
    from types import SimpleNamespace

    import iped_mcp.client as client_mod

    stub = SimpleNamespace(sleep=lambda _s: None, monotonic=real_time.monotonic)
    monkeypatch.setattr(client_mod, "time", stub)
