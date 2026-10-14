"""Contratos dos 13 endpoints de client.py travados contra o fake da 2d.

Cada teste pina o envelope/estado que o build em execucao na FASE 2d
realmente devolveu (vereditos WI-x). Testes marcados
``characterization`` registram comportamento HOJE-questionavel (doc->500,
404 cru que vaza) que M-2/M-8 vao trocar deliberadamente -- troque o pino
juntamente com a mudanca, nunca "conserte" o teste isoladamente.

M-2 (Design 02) aplicado: o client agora traduz TODO erro em
``iped_mcp.errors`` (sem texto requests cru); os pinos abaixo ja refletem
a taxonomia, mantendo o veredito de STATUS do jar (source->404,
doc->500) e a compatibilidade de classes (hierarquia herda de requests).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from iped_mcp import errors

from tests.conftest import (
    ALL_DOC_IDS,
    BOOKMARK_NAME_WITH_SPACE,
    CONTENT_FILENAME,
    DOC_MD5_UPPER,
    DOC_NO_DISPOSITION_ID,
    DOC_NO_IMAGE_ID,
    DOC_TEXT_ID,
    EXISTING_BOOKMARK,
    GRIZZLY_500,
    JPEG_MAGIC,
    MISSING_DOC_ID,
    NO_HITS_QUERY,
    SOURCE_ID,
    SOURCE_PATH,
    SOURCE_ID_2,
    TEXT_BODY,
    UNKNOWN_SOURCE,
)


def last_entry(api: Any, method: str, path: str) -> dict[str, Any]:
    """Ultima entrada do log do fake para (metodo, caminho).

    Args:
        api: Estado do ``FakeWebAPI`` (fixture ``webapi``).
        method: Verbo HTTP (ex.: ``"GET"``).
        path: Caminho decodificado (ex.: ``"/search"``).

    Returns:
        O dict de log da requisicao mais recente que casa.

    Raises:
        AssertionError: se nenhuma requisicao casa.
    """
    hits = [e for e in api.requests_log if e["method"] == method and e["path"] == path]
    assert hits, f"nenhum {method} {path} no log: {[e['path'] for e in api.requests_log]}"
    return hits[-1]


# --- #1 GET /sources/ (client.list_sources) --------------------------------


def test_list_sources_unwraps_data_envelope(client: Any, webapi: Any) -> None:
    """#1: corpo e {"data": [{id, path}]}; client devolve o conteudo de data."""
    sources = client.list_sources()
    body = json.loads(last_entry(webapi, "GET", "/sources/")["resp_body"])
    assert set(body) == {"data"}
    assert sources == [{"id": SOURCE_ID, "path": SOURCE_PATH},
                       {"id": SOURCE_ID_2, "path": "E:\\TESTE\\icloud"}]


def test_list_sources_body_has_no_pagination_fields(client: Any, webapi: Any) -> None:
    """#1: sem campo "total" (P1-1 ausente no jar, 2d 01) -- sem paginacao."""
    client.list_sources()
    assert b"total" not in last_entry(webapi, "GET", "/sources/")["resp_body"]


# --- #2 GET /search (client.search) -----------------------------------------


def test_search_returns_groups_source_ids(client: Any) -> None:
    """#2: envelope {"data"} achatado em grupos [{"source","ids"}] (WI-20)."""
    groups = client.search("*", source_id=SOURCE_ID)
    assert groups == [{"source": SOURCE_ID, "ids": ALL_DOC_IDS}]


def test_search_pins_query_parameters(client: Any, webapi: Any) -> None:
    """#2: URL e /search com q e sourceID exatamente como o client monta."""
    client.search("name:x", source_id=SOURCE_ID)
    entry = last_entry(webapi, "GET", "/search")
    assert entry["query"]["q"] == ["name:x"]
    assert entry["query"]["sourceID"] == [SOURCE_ID]


def test_search_omits_sourceid_when_none(client: Any, webapi: Any) -> None:
    """#2: sem source_id o parametro sourceID NAO e enviado (busca global)."""
    client.search("name:x")
    assert "sourceID" not in last_entry(webapi, "GET", "/search")["query"]


def test_search_no_hits_returns_empty_list(client: Any) -> None:
    """#2: sem hits o jar devolve {"data": []} (2d 01), nao erro."""
    assert client.search(NO_HITS_QUERY, source_id=SOURCE_ID) == []


def test_search_body_has_no_total_field(client: Any, webapi: Any) -> None:
    """#2: resposta SEM campo "total" -- capacidade de paginacao ausente.

    Pino da premissa WI-2/P1-1-inativo: /search devolve TODOS os ids;
    qualquer codigo que espere paginacao esta errado contra este jar.
    """
    client.search("*", source_id=SOURCE_ID)
    body = json.loads(last_entry(webapi, "GET", "/search")["resp_body"])
    assert set(body) == {"data"}
    assert set(body["data"][0]) == {"source", "ids"}


@pytest.mark.characterization
def test_search_unknown_source_raises_not_found(client: Any) -> None:
    """#2-wi3: source inexistente -> ResourceNotFoundError (HTTP 404).

    M-2 pinado: excecao propria com contexto (AC2/AC4); o jar ainda
    responde 404 text/plain (WI-3/P2-1), mas o texto requests cru nao vaza.
    """
    with pytest.raises(errors.ResourceNotFoundError) as exc:
        client.search("*", source_id=UNKNOWN_SOURCE)
    assert exc.value.status_code == 404
    assert exc.value.source_id == UNKNOWN_SOURCE
    assert exc.value.response is not None
    assert exc.value.response.text.startswith("source not found:")
    assert "for url:" not in str(exc.value)


def test_md5_search_resolves_doc_id(client: Any) -> None:
    """WI-18 (2d 3c, FACT): q="md5:<HASH maiusculo>" resolve id (case-insensitive)."""
    groups = client.search(f"md5:{DOC_MD5_UPPER}", source_id=SOURCE_ID)
    assert groups == [{"source": SOURCE_ID, "ids": [DOC_NO_IMAGE_ID]}]


def test_md5_search_sends_value_verbatim(client: Any, webapi: Any) -> None:
    """WI-18: o client envia o hash como recebido (sem lower/upper)."""
    client.search(f"md5:{DOC_MD5_UPPER}", source_id=SOURCE_ID)
    assert last_entry(webapi, "GET", "/search")["query"]["q"] == [f"md5:{DOC_MD5_UPPER}"]


# --- #3 GET /sources/{sid}/docs/{id} (client.get_document) ------------------


def test_get_document_is_at_top_level_no_envelope(client: Any, webapi: Any) -> None:
    """#3: doc vem NO TOPO, sem {"data"} (WI-1); props com valores em lista."""
    doc = client.get_document(SOURCE_ID, DOC_NO_IMAGE_ID)
    body = json.loads(last_entry(webapi, "GET", f"/sources/{SOURCE_ID}/docs/{DOC_NO_IMAGE_ID}")["resp_body"])
    assert "data" not in body
    assert doc["id"] == DOC_NO_IMAGE_ID
    assert doc["properties"]["md5"] == [DOC_MD5_UPPER]  # valor MAIUSCULO no jar


def test_get_document_injects_source_id_unquoted(client: Any, webapi: Any) -> None:
    """#3: path crua preserva '@' (ids reais funcionam sem quote; 2d Router)."""
    client.get_document(SOURCE_ID, DOC_NO_IMAGE_ID)
    assert f"/sources/{SOURCE_ID}/docs/{DOC_NO_IMAGE_ID}" in last_entry(
        webapi, "GET", f"/sources/{SOURCE_ID}/docs/{DOC_NO_IMAGE_ID}"
    )["raw_target"]


@pytest.mark.characterization
def test_get_document_missing_raises_server_error(client: Any, webapi: Any) -> None:
    """#3-wi3: doc inexistente -> ServerError (jar responde 500 Grizzly, NAO 404).

    M-2 pinado: 5xx chega como CLASSE DISTINTA de 404 (AC4, habilita
    M-3/M-8); mensagem com prefixo estavel "Doc nao encontrado" (AC5); a
    Response com o HTML Grizzly segue anexada (M-8 consome o corpo).
    """
    with pytest.raises(errors.ServerError) as exc:
        client.get_document(SOURCE_ID, MISSING_DOC_ID)
    assert exc.value.status_code == 500
    assert str(exc.value).startswith("Doc não encontrado")
    assert "for url:" not in str(exc.value)
    assert exc.value.response is not None
    assert GRIZZLY_500[:32] in exc.value.response.content


def test_get_document_unknown_source_raises_not_found(client: Any) -> None:
    """#3: source inexistente no path -> ResourceNotFoundError 404 (P2-1).

    M-2: 404 de SOURCE nao vira "Doc nao encontrado" — o 404 significa
    "nao existe" no path (fonte), independente de doc_id."""
    with pytest.raises(errors.ResourceNotFoundError) as exc:
        client.get_document(UNKNOWN_SOURCE, DOC_TEXT_ID)
    assert exc.value.status_code == 404


# --- #4/#5 GET /bookmarks[/nome] (list_bookmarks, get_bookmark_docs) --------


def test_list_bookmarks_unwraps_data_envelope(client: Any, webapi: Any) -> None:
    """#4: {"data": [nomes]}; client devolve a lista de nomes."""
    names = client.list_bookmarks()
    assert json.loads(last_entry(webapi, "GET", "/bookmarks")["resp_body"])["data"] == names
    assert names == [EXISTING_BOOKMARK]


def test_get_bookmark_unknown_returns_empty_200(client: Any, webapi: Any) -> None:
    """#5-PB2: bookmark inexistente -> 200 {"data": []}, nunca 404 (2d 09)."""
    docs = client.get_bookmark_docs("nao-existe-zz")
    entry = last_entry(webapi, "GET", "/bookmarks/nao-existe-zz")
    assert entry["status"] == 200
    assert docs == []


def test_get_bookmark_returns_source_id_groups(client: Any) -> None:
    """#5: forma real do payload e [{source, ids}] (2a/2c; payload NAO_TESTADO no jar)."""
    assert client.get_bookmark_docs(EXISTING_BOOKMARK) == [
        {"source": SOURCE_ID, "ids": [DOC_NO_IMAGE_ID]}
    ]


def test_get_bookmark_quotes_name_in_path(client: Any, webapi: Any) -> None:
    """#5: nomes com espaco passam por quote() (client.py:84) -> %20 na URL crua."""
    client.get_bookmark_docs(BOOKMARK_NAME_WITH_SPACE)
    assert "meu%20bookmark" in last_entry(
        webapi, "GET", "/bookmarks/meu bookmark"
    )["raw_target"]


# --- #6-#10 mutacoes de bookmark (contrato no nivel client, AC4) ------------


def test_create_bookmark_returns_200_empty_body_and_is_idempotent(client: Any, webapi: Any) -> None:
    """#6: POST 200 body vazio; criar de novo NAO erroa (docstring client.py:87)."""
    client.create_bookmark("novo-bm")
    entry = last_entry(webapi, "POST", "/bookmarks/novo-bm")
    assert entry["status"] == 200 and entry["resp_body"] == b""
    client.create_bookmark("novo-bm")
    assert "novo-bm" in client.list_bookmarks()


def test_add_to_bookmark_sends_pair_docs_and_ignores_missing(client: Any, webapi: Any) -> None:
    """#7: PUT [{source,id}]; doc inexistente ignorado em silencio (PB5)."""
    docs = [
        {"source": SOURCE_ID, "id": DOC_TEXT_ID},
        {"source": SOURCE_ID, "id": MISSING_DOC_ID},
    ]
    client.add_to_bookmark("bm-add", docs)
    entry = last_entry(webapi, "PUT", "/bookmarks/bm-add/add")
    assert entry["status"] == 200 and entry["resp_body"] == b""
    assert json.loads(entry["req_body"]) == docs
    assert client.get_bookmark_docs("bm-add") == [{"source": SOURCE_ID, "ids": [DOC_TEXT_ID]}]


def test_remove_from_bookmark_removes_only_wanted_docs(client: Any, webapi: Any) -> None:
    """#8: PUT remove tira so os docs pedidos; bookmark segue vivo."""
    client.remove_from_bookmark(EXISTING_BOOKMARK, [{"source": SOURCE_ID, "id": DOC_NO_IMAGE_ID}])
    assert last_entry(webapi, "PUT", f"/bookmarks/{EXISTING_BOOKMARK}/remove")["status"] == 200
    groups = client.get_bookmark_docs(EXISTING_BOOKMARK)
    assert groups == [{"source": SOURCE_ID, "ids": []}]


def test_rename_bookmark_moves_docs_to_new_name(client: Any, webapi: Any) -> None:
    """#9: PUT /bookmarks/{old}/rename/{new} (path com dois segmentos quoteados)."""
    client.rename_bookmark(EXISTING_BOOKMARK, "renomeado-y")
    entry = last_entry(webapi, "PUT", f"/bookmarks/{EXISTING_BOOKMARK}/rename/renomeado-y")
    assert entry["status"] == 200
    assert client.get_bookmark_docs("renomeado-y") == [
        {"source": SOURCE_ID, "ids": [DOC_NO_IMAGE_ID]}
    ]
    assert client.get_bookmark_docs(EXISTING_BOOKMARK) == []


def test_delete_bookmark_removes_it(client: Any, webapi: Any) -> None:
    """#10: DELETE 200 body vazio; o bookmark suma da listagem."""
    client.delete_bookmark(EXISTING_BOOKMARK)
    assert last_entry(webapi, "DELETE", f"/bookmarks/{EXISTING_BOOKMARK}")["status"] == 200
    assert EXISTING_BOOKMARK not in client.list_bookmarks()


# --- #11 GET /content (client.get_document_content) --------------------------


def test_content_filename_comes_from_content_disposition(client: Any) -> None:
    """#11-WI6: filename extraido de Content-Disposition filename="..." (client.py:137-141)."""
    filename, resp = client.get_document_content(SOURCE_ID, DOC_TEXT_ID)
    assert filename == CONTENT_FILENAME
    assert resp.content.startswith(JPEG_MAGIC)


def test_content_without_disposition_header_yields_empty_filename(client: Any) -> None:
    """#11: header ausente -> filename "" (contract do parse manual)."""
    filename, resp = client.get_document_content(SOURCE_ID, DOC_NO_DISPOSITION_ID)
    assert filename == ""
    assert resp.content  # body continua util


@pytest.mark.characterization
def test_content_missing_doc_raises_server_error(client: Any) -> None:
    """#11: doc inexistente -> ServerError traduzida (mesmo fixture Grizzly do #3)."""
    with pytest.raises(errors.ServerError) as exc:
        client.get_document_content(SOURCE_ID, MISSING_DOC_ID)
    assert exc.value.status_code == 500
    assert str(exc.value).startswith("Doc não encontrado")


# --- #12 GET /text (client.get_document_text) --------------------------------


def test_text_reads_full_stream_when_under_cap(client: Any) -> None:
    """#12-WI7: stream chunked lido ate o fim; capped=False, total==len(texto).

    M-8: retorno e TextResult; os 3 primeiros campos preservam o contrato
    legado (text, total_chars, capped) + status/total_bytes novos."""
    result = client.get_document_text(SOURCE_ID, DOC_TEXT_ID)
    assert result.capped is False
    assert result.text == TEXT_BODY.decode("utf-8")  # AC5: campo text intacto
    assert result.total_chars == len(result.text)
    assert result.status == "ok"
    assert result.total_bytes == len(TEXT_BODY)
    assert result[0] == result.text  # contrato legado: indice 0 ainda e o texto


def test_text_stops_at_max_bytes_and_flags_capped(client: Any) -> None:
    """#12: acima de max_bytes para de ler e marca capped=True (AC3)."""
    result = client.get_document_text(SOURCE_ID, DOC_TEXT_ID, max_bytes=1024)
    assert result.capped is True
    assert result.total_chars >= 1024
    assert result.status == "ok"
    assert result.total_bytes >= 1024


def test_text_missing_doc_becomes_no_text_status(client: Any) -> None:
    """#12/M-8 AC1: /text 500 (doc sem texto no jar) vira ESTADO, nao excecao.

    Substitui o pino de characterization do M-2 (500=ServerError como
    gancho do M-8): o gancho .response foi consumido — o 500 desta rota e
    o contrato WONT_FIX P2-5 do fork e devolve status no_text + texto vazio.
    """
    result = client.get_document_text(SOURCE_ID, MISSING_DOC_ID)
    assert result.status == "no_text"
    assert result.text == ""
    assert result.capped is False
    assert result.total_bytes == 0


def test_text_unknown_source_raises_not_found(client: Any) -> None:
    """#12: /text com source inexistente -> ResourceNotFoundError 404 (P2-1)."""
    with pytest.raises(errors.ResourceNotFoundError) as exc:
        client.get_document_text(UNKNOWN_SOURCE, DOC_TEXT_ID)
    assert exc.value.status_code == 404


# --- #13 GET /thumb (client.get_document_thumb) -------------------------------


def test_thumb_returns_raw_jpeg_bytes(client: Any) -> None:
    """#13-WI8: body e JPEG bruto (magic ff d8 ff e0) sem envelope."""
    resp = client.get_document_thumb(SOURCE_ID, DOC_TEXT_ID)
    assert resp.content.startswith(JPEG_MAGIC)


def test_thumb_empty_body_means_no_image(client: Any) -> None:
    """#13b: doc sem imagem -> 200 com body vazio (2d 27), nao erro."""
    resp = client.get_document_thumb(SOURCE_ID, DOC_NO_IMAGE_ID)
    assert resp.status_code == 200
    assert resp.content == b""


@pytest.mark.characterization
def test_thumb_missing_doc_raises_server_error(client: Any) -> None:
    """#13: doc inexistente -> ServerError (jar: 500 Grizzly)."""
    with pytest.raises(errors.ServerError) as exc:
        client.get_document_thumb(SOURCE_ID, MISSING_DOC_ID)
    assert exc.value.status_code == 500
