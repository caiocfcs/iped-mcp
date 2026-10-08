"""Tools de bookmarks (marcadores) do IPED.

O agente gerencia coleções de documentos sem conhecer as particularidades da
API (URL encoding, DocIDJSON[], tudo retornar 200 com body vazio, detecção de
"doc sem arquivo"). Lotes grandes de IDs entram via arquivo txt (full path).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from fastmcp import FastMCP

from iped_mcp.bookmarks.download import _first, download_bookmark, flatten_groups
from iped_mcp.bookmarks.export_info import export_bookmark_info
from iped_mcp.bookmarks.idsfile import read_ids_or_hashes_file
from iped_mcp.config import WORKERS
from iped_mcp.diagnose import diagnose_500
from iped_mcp.lucene import quote_value
from iped_mcp.router import get_router

_DOCS_LIMIT_DEFAULT = 50
_DOCS_LIMIT_MAX = 500
_SAVE_SEARCH_LIMIT_DEFAULT = 200
_SAVE_SEARCH_LIMIT_MAX = 1000
_REPORT_LIMIT_DEFAULT = 10
_REPORT_LIMIT_MAX = 50
_ADD_LIMIT_MAX = 500

_REPORT_TEXT_KEYS = ["audio:transcription", "dc:description", "text"]
_SEARCH_HINT = "Confira a sintaxe da query Lucene (ou use ufed_search/regex_*)."


def bookmark_names(source_id: str) -> list[str]:
    return get_router().list_bookmarks(source_id)


def bookmark_exists(name: str, source_id: str) -> bool:
    return name in get_router().list_bookmarks(source_id)


def _regroup(pairs: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """Lista plana (source, id) → grupos [{source, ids}] na ordem de aparição."""
    groups: dict[str, list[int]] = {}
    order: list[str] = []
    for source, doc_id in pairs:
        if source not in groups:
            groups[source] = []
            order.append(source)
        groups[source].append(doc_id)
    return [{"source": s, "ids": groups[s]} for s in order]


def _validate_docs(docs: list[dict[str, Any]]) -> None:
    if not docs:
        raise ValueError("'docs' não pode ser vazio.")
    for doc in docs:
        if not isinstance(doc, dict):
            raise ValueError("Cada doc deve ser um objeto {source, id}.")
        source = doc.get("source")
        doc_id = doc.get("id")
        if not isinstance(source, str) or not source:
            raise ValueError("Cada doc precisa de 'source' (str não vazia).")
        if not isinstance(doc_id, int) or isinstance(doc_id, bool):
            raise ValueError("Cada doc precisa de 'id' (int).")


def _fetch_docs_parallel(
    pairs: list[tuple[str, int]], workers: int = WORKERS
) -> list[tuple[str, int, dict | None]]:
    """Busca documentos em paralelo; devolve (source, doc_id, properties) na ordem.

    Doc que falhar vira (source, doc_id, None) e não derruba o lote.
    """
    client = get_router()

    def _fetch(pair: tuple[str, int]) -> tuple[str, int, dict | None]:
        source, doc_id = pair
        try:
            return source, doc_id, client.get_document(source, doc_id).get("properties", {})
        except (requests.RequestException, ValueError):
            return source, doc_id, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_fetch, pairs))


def _dedupe(seq: list[int]) -> list[int]:
    """Remove duplicados preservando a ordem de primeira aparição."""
    seen: set[int] = set()
    out: list[int] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _resolve_md5s(
    md5s: list[str], source: str
) -> tuple[list[int], list[str], list[dict[str, Any]]]:
    """Resolve MD5s -> doc IDs via busca `md5:<hash>` no source (paralelo).

    Devolve (ids únicos na ordem, md5s sem match, falhas [{md5, reason}]).
    Um MD5 pode casar com vários docs (duplicados); todos são incluídos.
    Falha em 1 busca não aborta o lote (contada em falhas).
    """
    if not md5s:
        return [], [], []
    client = get_router()

    def _resolve(md5: str) -> tuple[str, list[int], str | None]:
        try:
            groups = client.search(f"md5:{quote_value(md5)}", source_id=source)
            return md5, [i for g in groups for i in g.get("ids", [])], None
        except (requests.RequestException, ValueError) as exc:
            return md5, [], f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(_resolve, md5s))

    ids: list[int] = []
    seen: set[int] = set()
    not_found: list[str] = []
    failures: list[dict[str, Any]] = []
    for md5, doc_ids, reason in results:
        if reason is not None:
            failures.append({"md5": md5, "reason": reason})
        elif not doc_ids:
            not_found.append(md5)
        else:
            for doc_id in doc_ids:
                if doc_id not in seen:
                    seen.add(doc_id)
                    ids.append(doc_id)
    return ids, not_found, failures


def _summarize_doc(doc_id: int, source: str, props: dict) -> dict[str, Any]:
    item: dict[str, Any] = {"id": doc_id, "source": source}
    for key in ["name", "ext", "contentType", "created", "category"]:
        value = _first(props.get(key))
        if value is not None:
            item[key] = value
    size = _first(props.get("size"))
    if size is not None:
        try:
            item["size"] = int(size)
        except (TypeError, ValueError):
            pass
    path = _first(props.get("path"))
    if path is not None:
        item["path"] = str(path)[:120]
    for key in _REPORT_TEXT_KEYS:
        text = _first(props.get(key))
        if text is not None:
            item["text"] = str(text)[:200]
            break
    return item


# --- Tools ---


def bookmarks_list(source_id: str) -> list[str]:
    """Lista os nomes dos bookmarks no IPED.

    Args:
        source_id: source no IPED (veja iped_list_sources); bookmarks são
            por servidor, então o source define em qual servidor listar.
    """
    return get_router().list_bookmarks(source_id)


def bookmarks_docs(
    bookmark: str, source_id: str, limit: int = _DOCS_LIMIT_DEFAULT, offset: int = 0
) -> dict[str, Any]:
    """Lista os documentos de um bookmark (IDs por source), paginado.

    Retorna apenas IDs agrupados por source; use bookmarks_report para
    propriedades-chave e bookmarks_download para os arquivos. `total` é o nº
    total de docs do bookmark (use para decidir se refina com limit/offset).
    Bookmark inexistente → {"total": 0, "returned": 0, "docs": []}.

    Args:
        bookmark: nome do bookmark (veja bookmarks_list).
        source_id: source no IPED (veja iped_list_sources); bookmarks são
            por servidor, então o source define em qual servidor agir.
        limit: docs por página (padrão 50, máx. 500).
        offset: posição inicial da página (padrão 0).
    """
    if not 1 <= limit <= _DOCS_LIMIT_MAX:
        raise ValueError(f"limit deve estar entre 1 e {_DOCS_LIMIT_MAX}.")
    if offset < 0:
        raise ValueError("offset deve ser >= 0.")
    flat = flatten_groups(get_router().get_bookmark_docs(bookmark, source_id))
    total = len(flat)
    page = flat[offset : offset + limit]
    return {"total": total, "returned": len(page), "docs": _regroup(page)}


def bookmarks_create(name: str, source_id: str) -> dict[str, Any]:
    """Cria um bookmark vazio.

    O IPED não erroa se o bookmark já existe; a tool verifica na listagem e
    avisa em vez de chamar o servidor.

    Args:
        name: nome do bookmark (não vazio).
        source_id: source no IPED (veja iped_list_sources); bookmarks são
            por servidor, então o source define em qual servidor criar.
    """
    name = name.strip()
    if not name:
        raise ValueError("'name' não pode ser vazio.")
    if bookmark_exists(name, source_id):
        return {
            "created": name,
            "warning": f"Bookmark '{name}' já existe; nada foi criado.",
        }
    get_router().create_bookmark(name, source_id)
    return {"created": name}


def bookmarks_add(
    bookmark: str, docs: list[dict[str, Any]], source_id: str
) -> dict[str, Any]:
    """Adiciona documentos a um bookmark.

    `sent` = docs enviados; o IPED ignora silenciosamente docs inexistentes.
    Para lotes grandes (>500), use bookmarks_add_from_file (arquivo txt).
    Todos os docs devem ser do mesmo servidor que o source_id.

    Args:
        bookmark: nome do bookmark.
        docs: lista de {"source": str, "id": int} (máx. 500).
        source_id: source no IPED (veja iped_list_sources); define em qual
            servidor agir.
    """
    _validate_docs(docs)
    if len(docs) > _ADD_LIMIT_MAX:
        raise ValueError(
            f"{len(docs)} docs excede o teto de {_ADD_LIMIT_MAX} por chamada. "
            "Use bookmarks_add_from_file para lotes grandes."
        )
    client = get_router()
    existed = bookmark_exists(bookmark, source_id)
    client.add_to_bookmark(bookmark, docs, source_id)
    result: dict[str, Any] = {"sent": len(docs)}
    if not existed:
        result["warning"] = (
            f"Bookmark '{bookmark}' não existia antes; o IPED pode tê-lo criado "
            "ou ignorado (comportamento não testado). Confirme com bookmarks_docs."
        )
    return result


def bookmarks_remove(
    bookmark: str, docs: list[dict[str, Any]], source_id: str
) -> dict[str, Any]:
    """Remove documentos de um bookmark.

    `sent` = docs enviados; o IPED ignora silenciosamente docs inexistentes.
    Para lotes grandes (>500), use bookmarks_remove_from_file (arquivo txt).
    Todos os docs devem ser do mesmo servidor que o source_id.

    Args:
        bookmark: nome do bookmark.
        docs: lista de {"source": str, "id": int} (máx. 500).
        source_id: source no IPED (veja iped_list_sources); define em qual
            servidor agir.
    """
    _validate_docs(docs)
    if len(docs) > _ADD_LIMIT_MAX:
        raise ValueError(
            f"{len(docs)} docs excede o teto de {_ADD_LIMIT_MAX} por chamada. "
            "Use bookmarks_remove_from_file para lotes grandes."
        )
    get_router().remove_from_bookmark(bookmark, docs, source_id)
    return {"sent": len(docs)}


def bookmarks_rename(old_name: str, new_name: str, source_id: str) -> dict[str, Any]:
    """Renomeia um bookmark.

    Destrutiva para referências: quebra qualquer referência ao nome antigo —
    confirme com o usuário antes de chamar.

    Args:
        old_name: nome atual do bookmark (deve existir).
        new_name: novo nome (não pode já existir).
        source_id: source no IPED (veja iped_list_sources); define em qual
            servidor agir.
    """
    old_name = old_name.strip()
    new_name = new_name.strip()
    if not old_name or not new_name:
        raise ValueError("'old_name' e 'new_name' não podem ser vazios.")
    if old_name == new_name:
        raise ValueError("'old_name' e 'new_name' devem ser distintos.")
    names = bookmark_names(source_id)
    if old_name not in names:
        raise ValueError(f"Bookmark '{old_name}' não existe.")
    if new_name in names:
        raise ValueError(f"Bookmark '{new_name}' já existe.")
    get_router().rename_bookmark(old_name, new_name, source_id)
    return {"from": old_name, "to": new_name}


def bookmarks_delete(bookmark: str, source_id: str) -> dict[str, Any]:
    """Exclui um bookmark (não os documentos indexados).

    Destrutiva — confirme com o usuário antes de chamar.

    Args:
        bookmark: nome do bookmark (deve existir).
        source_id: source no IPED (veja iped_list_sources); define em qual
            servidor agir.
    """
    bookmark = bookmark.strip()
    if not bookmark:
        raise ValueError("'bookmark' não pode ser vazio.")
    if not bookmark_exists(bookmark, source_id):
        raise ValueError(f"Bookmark '{bookmark}' não existe.")
    get_router().delete_bookmark(bookmark, source_id)
    return {"deleted": bookmark}


def bookmarks_save_search(
    bookmark: str,
    query: str,
    source_id: str,
    limit: int = _SAVE_SEARCH_LIMIT_DEFAULT,
) -> dict[str, Any]:
    """Executa uma busca Lucene e salva os resultados num bookmark.

    Cria o bookmark se não existir e a busca retornar docs (created: true);
    busca sem resultados não cria nada. Salva até `limit` docs; se
    total_matches > limit, avisa para refinar a query. Domínios `ufed:` e
    `Regex:` têm tools próprias (ufed_search/regex_*); use-as para montar a
    query quando applicable.

    Args:
        bookmark: nome do bookmark.
        query: query Lucene (o agente monta).
        source_id: source no IPED (veja iped_list_sources); restringe a busca
            e define em qual servidor criar o bookmark.
        limit: máximo de docs a salvar (padrão 200, máx. 1000).
    """
    bookmark = bookmark.strip()
    if not bookmark:
        raise ValueError("'bookmark' não pode ser vazio.")
    if not query:
        raise ValueError("'query' não pode ser vazia.")
    if not 1 <= limit <= _SAVE_SEARCH_LIMIT_MAX:
        raise ValueError(f"limit deve estar entre 1 e {_SAVE_SEARCH_LIMIT_MAX}.")
    client = get_router()
    try:
        results = client.search(query, source_id=source_id)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 500:
            raise ValueError(diagnose_500(query, _SEARCH_HINT)) from exc
        raise
    except requests.ConnectionError as exc:
        raise ValueError(
            "Servidor IPED indisponível. Verifique se o IPED está rodando."
        ) from exc
    flat = flatten_groups(results)
    total_matches = len(flat)
    to_save = flat[:limit]
    created = False
    if to_save:
        if not bookmark_exists(bookmark, source_id):
            client.create_bookmark(bookmark, source_id)
            created = True
        client.add_to_bookmark(bookmark, [{"source": s, "id": i} for s, i in to_save], source_id)
    result: dict[str, Any] = {
        "saved": len(to_save),
        "total_matches": total_matches,
        "created": created,
    }
    if total_matches > limit:
        result["warning"] = (
            f"{total_matches} matches; só os {limit} primeiros foram salvos. "
            "Refine a query para salvar todos."
        )
    return result


def bookmarks_report(
    bookmark: str, source_id: str, limit: int = _REPORT_LIMIT_DEFAULT
) -> dict[str, Any]:
    """Amostra de documentos de um bookmark com propriedades-chave.

    Cada doc = 1 request HTTP; o teto baixo (50) evita martelar o IPED.
    `text` é o primeiro disponível entre audio:transcription, dc:description
    e text (trecho de até 200 chars). Docs que falham são omitidos e contados
    em `failed`.

    Args:
        bookmark: nome do bookmark.
        source_id: source no IPED (veja iped_list_sources); define em qual
            servidor agir.
        limit: nº de docs a amostrar (padrão 10, máx. 50).
    """
    if not 1 <= limit <= _REPORT_LIMIT_MAX:
        raise ValueError(f"limit deve estar entre 1 e {_REPORT_LIMIT_MAX}.")
    flat = flatten_groups(get_router().get_bookmark_docs(bookmark, source_id))
    total = len(flat)
    docs: list[dict[str, Any]] = []
    failed = 0
    for source, doc_id, props in _fetch_docs_parallel(flat[:limit]):
        if props is None:
            failed += 1
            continue
        docs.append(_summarize_doc(doc_id, source, props))
    return {"total": total, "failed": failed, "docs": docs}


def bookmarks_download(bookmark: str, dest_dir: str, source_id: str) -> dict[str, Any]:
    """Baixa todos os arquivos de um bookmark para uma pasta local.

    Sem teto de docs — o objetivo é salvar todos; bookmark grande pode levar
    minutos. Doc sem arquivo → skipped; falha em 1 doc não aborta o lote
    (reportada em failed/first_failures). O path é resolvido na máquina onde
    o MCP roda.

    Args:
        bookmark: nome do bookmark.
        dest_dir: pasta de destino (recomendado: caminho absoluto; criada se
            não existir).
        source_id: source no IPED (veja iped_list_sources); define em qual
            servidor agir.
    """
    bookmark = bookmark.strip()
    if not bookmark:
        raise ValueError("'bookmark' não pode ser vazio.")
    if not dest_dir:
        raise ValueError("'dest_dir' não pode ser vazio.")
    is_abs = os.path.isabs(dest_dir)
    target = os.path.abspath(os.path.expanduser(dest_dir))
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise ValueError(
            f"Não foi possível criar o diretório de destino '{target}': {e}"
        ) from e
    result = download_bookmark(bookmark, target, source_id)
    if not is_abs:
        result["warning"] = (
            f"dest_dir '{dest_dir}' é relativo e resolve contra o cwd do "
            "servidor (não do seu projeto). Use um caminho absoluto."
        )
    return result


def bookmarks_download_info(bookmark: str, dest_dir: str, source_id: str) -> dict[str, Any]:
    """Exporta todas as informações textuais estruturadas de um bookmark para um arquivo JSON.

    Um único JSON com todos os docs do bookmark: propriedades completas (todas as
    chaves, valores como o IPED retorna) e texto extraído (pode ser grande; a
    transcrição de áudio fica na propriedade audio:transcription). Sem teto de
    docs; falha em 1 doc não aborta o lote (reportada em failed/first_failures).
    O path é resolvido na máquina onde o MCP roda.

    Args:
        bookmark: nome do bookmark.
        dest_dir: pasta de destino (recomendado: caminho absoluto; criada se
            não existir).
        source_id: source no IPED (veja iped_list_sources); define em qual
            servidor agir.
    """
    bookmark = bookmark.strip()
    if not bookmark:
        raise ValueError("'bookmark' não pode ser vazio.")
    if not dest_dir:
        raise ValueError("'dest_dir' não pode ser vazio.")
    is_abs = os.path.isabs(dest_dir)
    target = os.path.abspath(os.path.expanduser(dest_dir))
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise ValueError(
            f"Não foi possível criar o diretório de destino '{target}': {e}"
        ) from e
    result = export_bookmark_info(bookmark, target, source_id)
    if not is_abs:
        result["warning"] = (
            f"dest_dir '{dest_dir}' é relativo e resolve contra o cwd do "
            "servidor (não do seu projeto). Use um caminho absoluto."
        )
    return result


def bookmarks_create_from_file(name: str, path: str, source: str) -> dict[str, Any]:
    """Cria um bookmark e adiciona os docs de um arquivo txt (1 ID ou MD5 por linha).

    Cada linha é um doc ID (só dígitos) ou um hash MD5 (32 hex); os MD5 são
    resolvidos para doc IDs via busca no source (um MD5 pode casar com vários
    docs). O path é resolvido na máquina onde o MCP roda. Se o bookmark já
    existe, erroa (use bookmarks_add_from_file para adicionar sem mesclar).

    Args:
        name: nome do bookmark (não pode já existir).
        path: full path do txt (1 doc ID ou MD5 por linha).
        source: source de todos os IDs/MD5 do arquivo.
    """
    name = name.strip()
    if not name:
        raise ValueError("'name' não pode ser vazio.")
    if not source:
        raise ValueError("'source' não pode ser vazio.")
    ids, md5s, invalid = read_ids_or_hashes_file(path)
    if not ids and not md5s:
        raise ValueError(f"Nenhum ID ou MD5 válido no arquivo '{path}'.")
    if bookmark_exists(name, source):
        raise ValueError(
            f"Bookmark '{name}' já existe. Use bookmarks_add_from_file para "
            "adicionar sem mesclar."
        )
    resolved, not_found, failures = _resolve_md5s(md5s, source)
    all_ids = _dedupe(ids + resolved)
    if not all_ids:
        raise ValueError(
            f"Nenhum doc encontrado no arquivo '{path}': os {len(md5s)} MD5 "
            "não casaram com nenhum doc do source."
        )
    client = get_router()
    client.create_bookmark(name, source)
    client.add_to_bookmark(name, [{"source": source, "id": i} for i in all_ids], source)
    result: dict[str, Any] = {
        "created": name,
        "added": len(all_ids),
        "ids": len(ids),
        "md5s": len(md5s),
        "md5_not_found": len(not_found),
        "invalid_lines": invalid,
    }
    if not_found:
        result["warning"] = (
            f"{len(not_found)} MD5 não casaram com nenhum doc: "
            f"{', '.join(not_found[:5])}" + ("…" if len(not_found) > 5 else "")
        )
    if failures:
        result["md5_failed"] = len(failures)
        result["first_failures"] = failures[:5]
    return result


def bookmarks_add_from_file(bookmark: str, path: str, source: str) -> dict[str, Any]:
    """Adiciona a um bookmark os docs de um arquivo txt (1 ID ou MD5 por linha).

    Cada linha é um doc ID (só dígitos) ou um hash MD5 (32 hex); os MD5 são
    resolvidos para doc IDs via busca no source. O path é resolvido na máquina
    onde o MCP roda. `sent` = docs enviados; o IPED ignora silenciosamente
    docs inexistentes (IDs).

    Args:
        bookmark: nome do bookmark.
        path: full path do txt (1 doc ID ou MD5 por linha).
        source: source de todos os IDs/MD5 do arquivo.
    """
    if not source:
        raise ValueError("'source' não pode ser vazio.")
    ids, md5s, invalid = read_ids_or_hashes_file(path)
    if not ids and not md5s:
        raise ValueError(f"Nenhum ID ou MD5 válido no arquivo '{path}'.")
    resolved, not_found, failures = _resolve_md5s(md5s, source)
    all_ids = _dedupe(ids + resolved)
    client = get_router()
    existed = bookmark_exists(bookmark, source)
    client.add_to_bookmark(bookmark, [{"source": source, "id": i} for i in all_ids], source)
    result: dict[str, Any] = {
        "sent": len(all_ids),
        "ids": len(ids),
        "md5s": len(md5s),
        "md5_not_found": len(not_found),
        "invalid_lines": invalid,
    }
    warnings: list[str] = []
    if not existed:
        warnings.append(
            f"Bookmark '{bookmark}' não existia antes; o IPED pode tê-lo criado "
            "ou ignorado (comportamento não testado). Confirme com bookmarks_docs."
        )
    if not_found:
        warnings.append(
            f"{len(not_found)} MD5 não casaram com nenhum doc: "
            f"{', '.join(not_found[:5])}" + ("…" if len(not_found) > 5 else "")
        )
    if warnings:
        result["warning"] = " ".join(warnings)
    if failures:
        result["md5_failed"] = len(failures)
        result["first_failures"] = failures[:5]
    return result


def bookmarks_remove_from_file(bookmark: str, path: str, source: str) -> dict[str, Any]:
    """Remove de um bookmark os docs de um arquivo txt (1 ID ou MD5 por linha).

    Cada linha é um doc ID (só dígitos) ou um hash MD5 (32 hex); os MD5 são
    resolvidos para doc IDs via busca no source. O path é resolvido na máquina
    onde o MCP roda. `sent` = docs enviados; o IPED ignora silenciosamente
    docs inexistentes (IDs).

    Args:
        bookmark: nome do bookmark.
        path: full path do txt (1 doc ID ou MD5 por linha).
        source: source de todos os IDs/MD5 do arquivo.
    """
    if not source:
        raise ValueError("'source' não pode ser vazio.")
    ids, md5s, invalid = read_ids_or_hashes_file(path)
    if not ids and not md5s:
        raise ValueError(f"Nenhum ID ou MD5 válido no arquivo '{path}'.")
    resolved, not_found, failures = _resolve_md5s(md5s, source)
    all_ids = _dedupe(ids + resolved)
    get_router().remove_from_bookmark(
        bookmark, [{"source": source, "id": i} for i in all_ids], source
    )
    result: dict[str, Any] = {
        "sent": len(all_ids),
        "ids": len(ids),
        "md5s": len(md5s),
        "md5_not_found": len(not_found),
        "invalid_lines": invalid,
    }
    if not_found:
        result["warning"] = (
            f"{len(not_found)} MD5 não casaram com nenhum doc: "
            f"{', '.join(not_found[:5])}" + ("…" if len(not_found) > 5 else "")
        )
    if failures:
        result["md5_failed"] = len(failures)
        result["first_failures"] = failures[:5]
    return result


def register(mcp: FastMCP) -> None:
    mcp.add_tool(bookmarks_list)
    mcp.add_tool(bookmarks_docs)
    mcp.add_tool(bookmarks_create)
    mcp.add_tool(bookmarks_add)
    mcp.add_tool(bookmarks_remove)
    mcp.add_tool(bookmarks_rename)
    mcp.add_tool(bookmarks_delete)
    mcp.add_tool(bookmarks_save_search)
    mcp.add_tool(bookmarks_report)
    mcp.add_tool(bookmarks_download)
    mcp.add_tool(bookmarks_download_info)
    mcp.add_tool(bookmarks_create_from_file)
    mcp.add_tool(bookmarks_add_from_file)
    mcp.add_tool(bookmarks_remove_from_file)
