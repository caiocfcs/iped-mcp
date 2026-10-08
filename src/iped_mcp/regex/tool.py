"""Tools de atributos `Regex:*` no IPED (detecção + estatísticas de frequência)."""

from __future__ import annotations

import csv
import os
from collections import Counter
from datetime import datetime
from typing import Any

from fastmcp import FastMCP

from iped_mcp.regex.queries import (
    KNOWN_TYPES,
    build_regex_query,
    normalize_attribute,
    normalize_value,
)
from iped_mcp.regex.scan import (
    discover_types,
    fetch_docs_parallel,
    scan_attribute,
    search_ids,
)

DEFAULT_EXPORTS_DIR = "/tmp/iped_mcp/exports"
EXPORTS_DIR = os.environ.get("IPED_MCP_EXPORTS_DIR", DEFAULT_EXPORTS_DIR)


def _first(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _counter(scan) -> Counter:
    return Counter(value for match in scan.matches for value in match.values)


def _distinct_normalized(counter: Counter, attribute: str) -> int:
    return len({normalize_value(value, attribute) for value in counter})


def _unknown_type_warning(attribute: str) -> str:
    return (
        f"Tipo '{attribute}' não está na lista conhecida ({', '.join(KNOWN_TYPES)}). "
        "Use regex_list_types para conferir os tipos disponíveis no source."
    )


def regex_list_types(source_id: str, sample_size: int = 200) -> dict[str, Any]:
    """Descobre os tipos `Regex:*` disponíveis no source (CNPJ, CPF, URL, etc.).

    Lista os tipos de dados detectados no source com o nº de docs em que cada
    um aparece. Tipos conhecidos têm contagem exata (how='exact'); tipos
    desconhecidos são descobertos por amostragem (how='sample', contagem
    estimada). A primeira chamada é lenta (busca match-all do source, ~1 min);
    as seguintes usam cache. Use antes de regex_stats/regex_top_values para
    conferir o nome do tipo (ex.: CNPJ é `BR_CNPJ`, não `CNPJ`).

    Args:
        source_id: source no IPED (veja iped_list_sources).
        sample_size: nº de documentos a amostrar p/ tipos desconhecidos (padrão 200, máx. 2000).
    """
    if not 1 <= sample_size <= 2000:
        raise ValueError("sample_size deve estar entre 1 e 2000.")
    result = discover_types(source_id, sample_size)
    return {
        "source": source_id,
        "types": result["types"],
        "sampled": result["sampled"],
        "note": (
            "how='exact': contagem exata de docs; how='sample': estimada a partir "
            f"de {result['sampled']} docs amostrados (tipos raros podem faltar; "
            "aumente sample_size)"
        ),
    }


def regex_stats(source_id: str, attribute: str) -> dict[str, Any]:
    """Conta matches e valores distintos de um tipo `Regex:*` no source.

    Ex.: BR_CNPJ pode retornar centenas de docs com milhares de matches e
    dezenas de valores distintos. Escaneia todos os docs onde o detector casou (1 GET/doc); para
    tipos grandes (ex.: URL, ~150 mil docs) leva vários minutos (~5-10 min).

    Args:
        source_id: source no IPED (veja iped_list_sources).
        attribute: tipo sem o prefixo (ex.: "BR_CNPJ"); prefixo "Regex:" é aceito.
    """
    attr = normalize_attribute(attribute)
    scan = scan_attribute(source_id, attr)
    counter = _counter(scan)
    result: dict[str, Any] = {
        "attribute": f"Regex:{attr}",
        "total_docs": scan.total_docs,
        "total_matches": sum(counter.values()),
        "distinct_bruto": len(counter),
        "distinct_normalizado": _distinct_normalized(counter, attr),
        "top": [{"value": v, "count": c} for v, c in counter.most_common(5)],
    }
    if scan.total_docs == 0 and attr not in KNOWN_TYPES:
        result["warning"] = _unknown_type_warning(attr)
    return result


def regex_top_values(source_id: str, attribute: str, top: int = 10) -> dict[str, Any]:
    """Top X valores mais frequentes de um tipo `Regex:*` no source.

    Ex.: top 10 CNPJs mais citados. Cada valor vem com a frequência e até 3
    docs de exemplo (id + name) para iniciar a investigação. Se top for maior
    ou igual ao nº de valores distintos, devolve todos os distintos.
    Escaneia todos os docs onde o detector casou (1 GET/doc); para tipos
    grandes (ex.: URL, ~150 mil docs) leva vários minutos (~5-10 min).

    Args:
        source_id: source no IPED (veja iped_list_sources).
        attribute: tipo sem o prefixo (ex.: "BR_CNPJ"); prefixo "Regex:" é aceito.
        top: nº de valores a devolver (padrão 10).
    """
    if top < 1:
        raise ValueError("top deve ser >= 1.")
    attr = normalize_attribute(attribute)
    scan = scan_attribute(source_id, attr)
    counter = _counter(scan)
    samples: dict[str, list[dict[str, Any]]] = {}
    for match in scan.matches:
        for value in match.values:
            bucket = samples.setdefault(value, [])
            if len(bucket) < 3:
                bucket.append({"id": match.doc_id, "name": match.name})
    return {
        "attribute": f"Regex:{attr}",
        "total_docs": scan.total_docs,
        "total_matches": sum(counter.values()),
        "distinct_bruto": len(counter),
        "top": [
            {"value": v, "count": c, "sample_docs": samples[v]}
            for v, c in counter.most_common(top)
        ],
    }


def regex_export_csv(source_id: str, attribute: str, output_dir: str | None = None) -> dict[str, Any]:
    """Exporta a tabela de frequência de um tipo `Regex:*` como CSV.

    Formato longo: 1 linha por match (doc x valor), colunas
    doc_id,name,md5,attribute,valor,frequencia. O cabeçalho do arquivo tem
    metadados em linhas `#` (source, attribute, data, totais, distinct).
    O arquivo é gravado em output_dir (ou $IPED_MCP_EXPORTS_DIR, ou
    /tmp/iped_mcp/exports) e o caminho absoluto é devolvido. Escaneia todos
    os docs onde o detector casou (1 GET/doc); para tipos grandes (ex.: URL,
    ~150 mil docs) leva vários minutos (~5-10 min).

    Args:
        source_id: source no IPED (veja iped_list_sources).
        attribute: tipo sem o prefixo (ex.: "BR_CNPJ"); prefixo "Regex:" é aceito.
        output_dir: pasta de destino do CSV (recomendado: caminho absoluto da
            pasta exports/ do seu projeto; sem o parâmetro, grava em
            /tmp/iped_mcp/exports).
    """
    attr = normalize_attribute(attribute)
    scan = scan_attribute(source_id, attr)
    counter = _counter(scan)
    now = datetime.now()
    target = str(output_dir) if output_dir else EXPORTS_DIR
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise ValueError(f"Não foi possível criar o diretório de exportação '{target}': {e}") from e
    path = os.path.abspath(
        os.path.join(target, f"regex_{source_id}_{attr}_{now:%Y%m%d-%H%M%S}.csv")
    )
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# source: {source_id}\n")
        f.write(f"# attribute: Regex:{attr}\n")
        f.write(f"# generated_at: {now.isoformat(timespec='seconds')}\n")
        f.write(f"# total_docs: {scan.total_docs}\n")
        f.write(f"# total_matches: {sum(counter.values())}\n")
        f.write(f"# distinct_bruto: {len(counter)}\n")
        f.write(f"# distinct_normalizado: {_distinct_normalized(counter, attr)}\n")
        writer = csv.writer(f)
        writer.writerow(["doc_id", "name", "md5", "attribute", "valor", "frequencia"])
        for match in scan.matches:
            for value in match.values:
                writer.writerow(
                    [match.doc_id, match.name or "", match.md5 or "", f"Regex:{attr}", value, counter[value]]
                )
    result: dict[str, Any] = {
        "path": path,
        "rows": sum(counter.values()),
        "total_docs": scan.total_docs,
        "distinct_bruto": len(counter),
    }
    if output_dir is None:
        result["warning"] = (
            f"CSV gravado no diretório padrão {target}. "
            "Para gravar no seu projeto, chame novamente passando output_dir "
            "com o caminho absoluto da pasta exports/."
        )
    elif not os.path.isabs(target):
        result["warning"] = (
            f"output_dir '{output_dir}' é relativo e resolve contra o cwd do "
            "servidor (não do seu projeto). Use um caminho absoluto."
        )
    return result


def regex_value_docs(
    source_id: str, attribute: str, value: str, limit: int = 50
) -> dict[str, Any]:
    """Docs onde um valor específico de um tipo `Regex:*` aparece.

    Drill-down: após regex_top_values, investiga um valor específico
    (ex.: um CNPJ). O valor pode ser exato ("12.345.678/0001-90") ou um
    fragmento ("12"). Devolve id, name e md5 de até `limit` docs.

    Args:
        source_id: source no IPED (veja iped_list_sources).
        attribute: tipo sem o prefixo (ex.: "BR_CNPJ"); prefixo "Regex:" é aceito.
        value: valor exato ou fragmento a buscar.
        limit: máximo de documentos a detalhar (padrão 50).
    """
    if not value:
        raise ValueError("'value' não pode ser vazio.")
    attr = normalize_attribute(attribute)
    ids = search_ids(build_regex_query(attr, value=value), source_id)
    docs = []
    for doc_id, props in fetch_docs_parallel(source_id, ids[:limit]):
        if not props:
            continue
        md5 = _first(props.get("md5"))
        docs.append(
            {
                "id": doc_id,
                "name": _first(props.get("name")),
                "md5": str(md5).lower() if md5 is not None else None,
            }
        )
    return {"attribute": f"Regex:{attr}", "value": value, "total": len(ids), "docs": docs}


def register(mcp: FastMCP) -> None:
    mcp.add_tool(regex_list_types)
    mcp.add_tool(regex_stats)
    mcp.add_tool(regex_top_values)
    mcp.add_tool(regex_export_csv)
    mcp.add_tool(regex_value_docs)
