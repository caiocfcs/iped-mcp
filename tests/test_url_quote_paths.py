"""Quote() de source/doc/bookmark ids em todas as paths de URL (M-13).

Pinha os tres contratos do item: (1) toda path montada pelo cliente passa
pela MESMA funcao de codificacao (`_quote_path_id`, urllib.parse.quote) —
ids com espaco, '%' e acento produzem a URL final correta; (2) aplicacao
unica por id — o `raw_target` que chega ao servidor e exatamente UMA
codificacao do id cru (requests nao re-escapa o `%XX` produzido, entao
nao ha dupla escapagem no pipeline); (3) no-op byte-a-byte para o alfabeto
observado (e-mails com `@`, `.`, `-`, digitos) — os 20 ids de referencia
permanecem com a URL integral identica ao baseline (originais da 2d,
medidos fora do repositorio, foram sanitizados na fase 11).
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from iped_mcp.client import _quote_path_id
from tests.conftest import SOURCE_ID, SOURCE_ID_2, make_doc
from tests.test_webapi_contract import last_entry

# Os 20 ids de source de referencia (amostra da FASE 2d, hoje sanitizada e
# substituida por equivalentes sinteticos com o mesmo alfabeto/shape).
# Nenhum contem '%'; todos devem seguir byte-a-byte identicos na URL crua
# (no-op do quote para o alfabeto observado).
REAL_SOURCE_IDS = (
    "pessoa_teste_13@example.invalid",
    "pessoa_teste_15@example.invalid",
    "pessoa_teste_20@example.invalid",
    "pessoa_teste_7@example.invalid",
    "pessoa_teste_8@example.invalid",
    "pessoa_teste_6@example.invalid",
    "pessoa_teste_10@example.invalid",
    "pessoa_teste_16@example.invalid",
    "pessoa_teste_17@example.invalid",
    "pessoa_teste_5@example.invalid",
    "pessoa_teste_14@example.invalid",
    "pessoa_teste_23@example.invalid",
    "pessoa_teste_12@example.invalid",
    "pessoa_teste_21@example.invalid",
    "pessoa_teste_22@example.invalid",
    "pessoa_teste_2@example.invalid",
    "pessoa_teste_11@example.invalid",
    "pessoa_teste_18@example.invalid",
    "pessoa_teste_19@example.invalid",
    "pessoa_teste_4@example.invalid-0000001",
)

# (id cru, codificacao esperada na URL) — casos do prompt da fase: espaco,
# '%', acento e id normal. Os dois ultimos sao o alfabeto real da 2d.
_ENCODED_CASES = [
    pytest.param("meu source", "meu%20source", id="espaco"),
    pytest.param("100%case", "100%25case", id="percentual"),
    pytest.param("café-açaí", "caf%C3%A9-a%C3%A7a%C3%AD", id="acentos"),
    pytest.param(SOURCE_ID, SOURCE_ID, id="email-sintetico"),
    pytest.param(SOURCE_ID_2, SOURCE_ID_2, id="email-sintetico-hifen"),
]

_DOC_ID = 3

# 4 rotas de doc do client: sufixo de path -> chamada a exercer.
_DOC_CALLS: dict[str, Callable[[Any, str], None]] = {
    "": lambda c, sid: c.get_document(sid, _DOC_ID) and None,
    "text": lambda c, sid: c.get_document_text(sid, _DOC_ID) and None,
    "content": lambda c, sid: c.get_document_content(sid, _DOC_ID)[1].close(),
    "thumb": lambda c, sid: c.get_document_thumb(sid, _DOC_ID).close(),
}


def _register_source_doc(webapi: Any, source_id: str) -> None:
    """Registra source + doc no fake para a rota responder 200.

    Args:
        webapi: estado do FakeWebAPI (fixture ``webapi``).
        source_id: id cru do source a cadastrar (com o doc ``_DOC_ID``).
    """
    webapi.sources.append({"id": source_id, "path": "X"})
    webapi.docs[(source_id, _DOC_ID)] = make_doc(_DOC_ID, "arquivo.bin", {})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("meu doc", "meu%20doc", id="espaco"),
        pytest.param("100%case", "100%25case", id="percentual"),
        pytest.param("café", "caf%C3%A9", id="acento"),
        pytest.param(SOURCE_ID, SOURCE_ID, id="email-no-op"),
        pytest.param("arquivo_final-v2.bin", "arquivo_final-v2.bin", id="comum-no-op"),
        pytest.param(42, "42", id="inteiro"),
        pytest.param("a/b", "a%2Fb", id="barra-escapada"),
        pytest.param("a?b#c", "a%3Fb%23c", id="query-fragment"),
    ],
)
def test_quote_path_id_table(raw: Any, expected: str) -> None:
    """Unidade: `_quote_path_id` aplica exatamente urllib.parse.quote(safe='@')."""
    assert _quote_path_id(raw) == expected


@pytest.mark.parametrize("raw_id,encoded", _ENCODED_CASES)
@pytest.mark.parametrize(
    "subview", ["", "text", "content", "thumb"], ids=["doc", "text", "content", "thumb"]
)
def test_doc_paths_quote_ids_exactly_once(
    client: Any, webapi: Any, raw_id: str, encoded: str, subview: str
) -> None:
    """docs/text/content/thumb: URL crua == UMA codificacao do id (M-13).

    A igualdade com o alvo esperado (em vez de substring) prova a aplicacao
    unica: dupla escapagem do cliente ou re-codificacao do requests
    produziria alvo diferente ('%2520' em vez de '%20', por exemplo).
    """
    _register_source_doc(webapi, raw_id)
    _DOC_CALLS[subview](client, raw_id)
    suffix = f"/{subview}" if subview else ""
    entry = last_entry(webapi, "GET", f"/sources/{raw_id}/docs/{_DOC_ID}{suffix}")
    assert entry["raw_target"] == f"/sources/{encoded}/docs/{_DOC_ID}{suffix}"


def test_percent_literal_in_raw_id_survives_single_escape(
    client: Any, webapi: Any
) -> None:
    """Id cru contendo '%20' literal vira '%2520' UMA vez e e decodificado
    de volta pelo servidor: o contrato e receber ids crus (fonte: JSON da
    WebAPI); nao ha caminho interno que realimenta id ja-quoted.
    """
    raw_id = "a%20b"
    _register_source_doc(webapi, raw_id)
    doc = client.get_document(raw_id, _DOC_ID)  # fake decodifica 1x e acha o doc
    assert doc["id"] == _DOC_ID
    assert last_entry(webapi, "GET", f"/sources/{raw_id}/docs/{_DOC_ID}")[
        "raw_target"
    ] == "/sources/a%2520b/docs/3"


def test_all_20_real_source_ids_are_byte_identical(
    client: Any, webapi: Any
) -> None:
    """Pino de regressao: os 20 ids de referencia seguem com URL byte-identica.

    O quote() com safe='@' e no-op para o alfabeto observado (letras,
    digitos, '.', '-', '_', '@') — nenhuma URL enviada hoje ao jar-alvo
    muda de bytes com o M-13.
    """
    for sid in REAL_SOURCE_IDS:
        _register_source_doc(webapi, sid)
    for sid in REAL_SOURCE_IDS:
        for subview, call in _DOC_CALLS.items():
            call(client, sid)
            suffix = f"/{subview}" if subview else ""
            expected = f"/sources/{sid}/docs/{_DOC_ID}{suffix}"
            assert last_entry(
                webapi, "GET", f"/sources/{sid}/docs/{_DOC_ID}{suffix}"
            )["raw_target"] == expected


def test_bookmark_paths_use_the_same_quoting(client: Any, webapi: Any) -> None:
    """Os 6 endpoints de bookmark adotam a mesma funcao (fonte unica, M-13).

    Nomes com espaco + '%' + acento produzem a URL crua codificada uma
    unica vez, inclusive nos dois segmentos do rename.
    """
    name = "meu bookmark 100% café"
    enc = "meu%20bookmark%20100%25%20caf%C3%A9"
    client.get_bookmark_docs(name)
    assert last_entry(webapi, "GET", f"/bookmarks/{name}")["raw_target"] == (
        f"/bookmarks/{enc}"
    )
    client.create_bookmark(name)
    assert last_entry(webapi, "POST", f"/bookmarks/{name}")["raw_target"] == (
        f"/bookmarks/{enc}"
    )
    client.add_to_bookmark(name, [])
    assert last_entry(webapi, "PUT", f"/bookmarks/{name}/add")["raw_target"] == (
        f"/bookmarks/{enc}/add"
    )
    client.remove_from_bookmark(name, [])
    assert last_entry(webapi, "PUT", f"/bookmarks/{name}/remove")["raw_target"] == (
        f"/bookmarks/{enc}/remove"
    )
    client.rename_bookmark(name, "novo nome")
    assert last_entry(
        webapi, "PUT", f"/bookmarks/{name}/rename/novo nome"
    )["raw_target"] == f"/bookmarks/{enc}/rename/novo%20nome"
    client.delete_bookmark(name)
    assert last_entry(webapi, "DELETE", f"/bookmarks/{name}")["raw_target"] == (
        f"/bookmarks/{enc}"
    )
