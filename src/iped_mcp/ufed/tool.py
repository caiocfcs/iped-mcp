"""Tools de busca de atributos `ufed:` no IPED."""

from __future__ import annotations

from typing import Any

import requests
from fastmcp import FastMCP

from iped_mcp.diagnose import diagnose_500
from iped_mcp.fetch import cache_get, cache_set, fetch_docs_parallel
from iped_mcp.router import get_router
from iped_mcp.ufed.queries import (
    INDEXED_FIELDS,
    NON_INDEXED_FIELDS,
    build_ufed_query,
    normalize_attribute,
    read_ufed_props,
)

_UFED_HINT = "Use ufed_list_fields para conferir os campos válidos."


def _first(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def ufed_search(
    source_id: str,
    attribute: str,
    value: str | None = None,
    values: list[str] | None = None,
    extra_filters: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Busca documentos por atributo `ufed:` (o escape `ufed\\:` é tratado internamente).

    Campos válidos: EntryName, EntryValue, EntryCategory, Source, extractionName,
    Name, URL, ChangeTime, decoding_confidence, isrelated, fs, MD5, SHA256.
    O valor é case-insensitive; o wildcard "*" funciona no valor.
    Retorna apenas id, name e o campo buscado (+ ufed:EntryValue quando
    attribute é EntryName); use get_document para o documento completo.

    Args:
        source_id: source no IPED (veja iped_list_sources).
        attribute: campo `ufed:` sem o prefixo (ex.: "EntryName"); prefixo "ufed:" é aceito.
        value: valor a buscar; "*" = todos (padrão).
        values: alternativa a value: OR dentro do mesmo atributo.
        extra_filters: filtros Lucene normais combinados com AND (ex.: 'category:"Device Information"').
        limit: máximo de documentos a detalhar (padrão 50, teto 500).
    """
    attr = normalize_attribute(attribute)
    if attr not in INDEXED_FIELDS:
        raise ValueError(
            f"Campo 'ufed:{attr}' não é indexado no IPED. "
            f"Campos válidos: {', '.join(INDEXED_FIELDS)}. "
            f"Não indexados (retornam erro): {', '.join(NON_INDEXED_FIELDS)}."
        )
    if not 1 <= limit <= 500:
        raise ValueError("limit deve estar entre 1 e 500.")
    query = build_ufed_query(attr, value=value, values=values, extra_filters=extra_filters)
    client = get_router()
    try:
        results = client.search(query, source_id=source_id)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 500:
            raise ValueError(diagnose_500(query, _UFED_HINT)) from exc
        raise
    except requests.ConnectionError as exc:
        raise ValueError(
            "Servidor IPED indisponível. Verifique se o IPED está rodando."
        ) from exc
    ids = [doc_id for group in results for doc_id in group.get("ids", [])]
    docs = []
    for doc_id, props in fetch_docs_parallel(source_id, ids[:limit]):
        if props is None:
            continue
        item: dict[str, Any] = {"id": doc_id, "name": _first(props.get("name"))}
        ufed_value = read_ufed_props({"properties": props}, attr)
        if ufed_value is not None:
            item[f"ufed:{attr}"] = ufed_value
        if attr == "EntryName":
            entry_value = read_ufed_props({"properties": props}, "EntryValue")
            if entry_value is not None:
                item["ufed:EntryValue"] = entry_value
        docs.append(item)
    return {"total": len(ids), "docs": docs}


def ufed_device_info(source_id: str) -> dict[str, Any]:
    """Device Information do source: tabela nome/valor (IMEI, ICCID, IMSI, MSISDN, Serial, modelo, SO, Apple ID, operadora, etc.).

    Equivale a ufed_search(source_id, attribute="EntryName", value="*").
    Dado estável (metadados do dispositivo) → cache de 5 min por source.

    Args:
        source_id: source no IPED (veja iped_list_sources).
    """
    key = ("ufed_device_info", source_id)
    cached = cache_get(key)
    if cached is not None:
        return cached
    result = ufed_search(source_id, attribute="EntryName", value="*", limit=200)
    items = [
        {"name": doc.get("ufed:EntryName"), "value": doc.get("ufed:EntryValue")}
        for doc in result["docs"]
        if doc.get("ufed:EntryName") is not None
    ]
    out = {"total": result["total"], "items": items}
    cache_set(key, out)
    return out


def ufed_list_fields() -> dict[str, Any]:
    """Referência dos campos `ufed:` no IPED: quais são buscáveis (indexados) e quais não são (retornam HTTP 500).

    Use em dúvida sobre o campo antes de chamar ufed_search.
    """
    return {
        "indexed": INDEXED_FIELDS,
        "not_indexed": {
            "fields": NON_INDEXED_FIELDS,
            "note": "não indexados: a busca retorna HTTP 500 mesmo com escape correto.",
        },
    }


def get_document(source_id: str, doc_id: int) -> dict[str, Any]:
    """Retorna as properties completas de um documento do source.

    Use para inspecionar em detalhe um documento encontrado via ufed_search.

    Args:
        source_id: source no IPED (veja iped_list_sources).
        doc_id: id do documento (campo "id" do retorno de ufed_search).
    """
    doc = get_router().get_document(source_id, doc_id)
    return doc.get("properties", doc)


def register(mcp: FastMCP) -> None:
    mcp.add_tool(ufed_search)
    mcp.add_tool(ufed_device_info)
    mcp.add_tool(ufed_list_fields)
    mcp.add_tool(get_document)
