"""Tools de busca de atributos `ufed:` no IPED."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from iped_mcp import validation
from iped_mcp.diagnose import guarded_call
from iped_mcp.fetch import cache_get, cache_set, fetch_docs_parallel, search_ids
from iped_mcp.router import get_router
from iped_mcp.ufed.queries import (
    KNOWN_FIELDS,
    build_ufed_query,
    field_advisory,
    normalize_attribute,
    observed_field_names,
    read_ufed_props,
    zero_hits_advisory,
)

_UFED_HINT = (
    "Use ufed_list_fields(source_id=...) para ver os campos conhecidos e os "
    "observados em amostra deste source."
)
# Match-all da amostra é cara (~47-66 s no source de teste; regex/scan.py:16);
# mesmo timeout das demais match-all do projeto (regex/scan.py:17, core 120 s).
_MATCH_ALL_QUERY = "*"
_MATCH_ALL_TIMEOUT = 120.0
_DEFAULT_SAMPLE_SIZE = 10


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

    A lista de campos conhecidos é ADVERTÊNCIA, não validação: campos fora
    dela são buscados normalmente (o IPED nunca retorna erro para campo
    desconhecido — devolve hits ou 0 hits em silêncio). Busca com campo fora
    da lista traz aviso em `warnings`, e 0 hits nesse caso ganha aviso extra
    "o campo pode não existir neste índice" (falso negativo mudo).
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

    Returns:
        {total, docs, warnings} — `warnings` sempre presente, vazio quando
        não há nada a advertir.

    Raises:
        ValueError: `limit` fora de 1..500, ou erro de busca (source
            indisponível/inexistente, query rejeitada com HTTP 500).
    """
    attr = normalize_attribute(attribute)
    warnings: list[str] = []
    advisory = field_advisory(attr)
    if advisory is not None:
        warnings.append(advisory)
    validation.check_int_range(limit, "limit", 1, validation.DOCS_LIMIT_MAX)
    query = build_ufed_query(attr, value=value, values=values, extra_filters=extra_filters)
    client = get_router()
    # Politica unica de erro (M-2): guard compartilhado; 500 → diagnose com a
    # query montada; rede/timeout/404 → textos estáveis de iped_mcp.errors.
    results = guarded_call(client.search, query, _UFED_HINT, query, source_id=source_id)
    ids = [doc_id for group in results for doc_id in group.get("ids", [])]
    if not ids:
        zero_hits = zero_hits_advisory(attr)
        if zero_hits is not None:
            warnings.append(zero_hits)
    docs = _project_docs(source_id, ids, limit, attr)
    return {"total": len(ids), "docs": docs, "warnings": warnings}


def _project_docs(
    source_id: str, ids: list[int], limit: int, attr: str
) -> list[dict[str, Any]]:
    """Projeta os hits no retorno compacto da tool (id, name, campo buscado).

    Args:
        source_id: source roteado para o fetch de properties.
        ids: doc ids da busca (o fetch detalha só os `limit` primeiros).
        limit: teto de documentos detalhados.
        attr: campo buscado (sem prefixo); EntryName puxa EntryValue junto.

    Returns:
        Um item {id, name, ufed:<attr>?} por doc resolvido; docs cuja
        leitura falhou sao omitidos (o fetch paralelo devolve None).
    """
    docs: list[dict[str, Any]] = []
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
    return docs


def ufed_device_info(source_id: str) -> dict[str, Any]:
    """Device Information do source: tabela nome/valor (IMEI, ICCID, IMSI, MSISDN, Serial, modelo, SO, Apple ID, operadora, etc.).

    Equivale a ufed_search(source_id, attribute="EntryName", value="*").
    Dado estável (metadados do dispositivo) → cache de 5 min por source.

    Args:
        source_id: source no IPED (veja iped_list_sources).

    Returns:
        {total, items: [{name, value}], warnings}.
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
    out = {"total": result["total"], "items": items, "warnings": result["warnings"]}
    cache_set(key, out)
    return out


def _stratified_sample(ids: list[int], n: int) -> list[int]:
    """Escolhe n ids espaçados ao longo da lista (não só os primeiros).

    Cópias locais de regex/scan.py:79-84 até haver helper compartilhado:
    começar a lista sem amostragem estratificada pode ver só um tipo de
    arquivo e esconder campos presentes no resto do caso.

    Args:
        ids: Ids de todos os hits.
        n: Tamanho desejado da amostra.

    Returns:
        Até n ids espaçados uniformemente (todos, se len(ids) <= n).
    """
    if len(ids) <= n:
        return list(ids)
    step = len(ids) / n
    return [ids[int(i * step)] for i in range(n)]


def _observed_fields_sample(source_id: str, sample_size: int) -> tuple[list[str], int]:
    """Descobre por evidência os campos `ufed:` presentes em um source.

    Match-all + amostra estratificada de `sample_size` docs + leitura
    paralela de properties (descoberta-por-evidência em versão mínima,
    M-7/DESIGN 07). Cache ~5 min por (source, amostra) — a match-all é cara.

    Args:
        source_id: source no IPED.
        sample_size: nº de docs da amostra.

    Returns:
        (Campos observados ordenados, nº de docs efetivamente amostrados).

    Raises:
        ValueError: source indisponível/inexistente ou match-all rejeitada
            (HTTP 500) — falha de coleta não vira lista silenciosa.
    """
    key = ("ufed_fields_sample", source_id, sample_size)
    cached = cache_get(key)
    if cached is not None:
        return cached
    all_ids = search_ids(_MATCH_ALL_QUERY, source_id, _UFED_HINT, timeout=_MATCH_ALL_TIMEOUT)
    sampled_ids = _stratified_sample(all_ids, sample_size)
    observed: set[str] = set()
    for _doc_id, props in fetch_docs_parallel(source_id, sampled_ids):
        if props:
            observed.update(observed_field_names(props))
    result = (sorted(observed), len(sampled_ids))
    cache_set(key, result)
    return result


def ufed_list_fields(
    source_id: str | None = None,
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
) -> dict[str, Any]:
    """Referência dos campos `ufed:`: lista conhecida + descoberta leve por amostra.

    A lista conhecida é pontapé ADVISORY (M-7): o índice real varia por build
    do IPED e o jar nunca erro para campo fora dela — por isso, informando
    `source_id`, a resposta MESCLA a base com os campos realmente observados
    numa amostra de documentos deste source (evidência > suposição).

    Args:
        source_id: se informado, amostra docs deste source (busca match-all
            + N leituras de properties, cache ~5 min).
        sample_size: nº de docs da amostra (1..200, padrão 10).

    Returns:
        {known_fields, observed_fields, fields (base ∪ observados), sampled,
        warnings} — sem promessa de exaustividade: campos fora de `fields`
        também podem existir e ser buscáveis.

    Raises:
        ValueError: `sample_size` fora de 1..200 com `source_id`, ou erro
            de busca/fetch durante a amostragem.
    """
    warnings: list[str] = []
    observed: list[str] = []
    sampled = 0
    if source_id is not None:
        validation.check_int_range(
            sample_size, "sample_size", 1, validation.FIELD_SAMPLE_SIZE_MAX
        )
        observed, sampled = _observed_fields_sample(source_id, sample_size)
        if sampled == 0:
            warnings.append(
                "A amostra deste source não retornou documentos: apenas a "
                "lista conhecida pôde ser devolvida."
            )
    merged = list(KNOWN_FIELDS) + [f for f in observed if f not in KNOWN_FIELDS]
    return {
        "known_fields": list(KNOWN_FIELDS),
        "observed_fields": observed,
        "fields": merged,
        "sampled": sampled,
        "warnings": warnings,
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
