"""Exportação de informações estruturadas de um bookmark (JSON).

M-10 (DESIGN 10): artefato imutável e auto-descritivo — nome
`<UTC-ts>_<source>_<bookmark>.json` gravado pelo helper único
`iped_mcp.download.export_path` (sem sobrescrita: colisão ganha `-N`),
`generated_at` UTC e docs NA ORDEM DA ENTRADA do bookmark (AC4).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from iped_mcp.bookmarks.download import flatten_groups
from iped_mcp.config import WORKERS
from iped_mcp.download import export_path, iso_utc, utc_now
from iped_mcp.router import get_router


def value_chars(value: Any) -> int:
    """Chars de um valor: str → len; list → soma dos elementos; outro → len(str)."""
    if isinstance(value, list):
        return sum(len(str(item)) for item in value)
    return len(str(value))


def _props_chars(props: dict[str, Any]) -> int:
    return sum(value_chars(v) for v in props.values())


def _fetch_doc(pair: tuple[str, int]) -> dict[str, Any]:
    """→ entry do doc; falha em properties → error; falha em text → text.error."""
    source, doc_id = pair
    client = get_router()
    entry: dict[str, Any] = {"id": doc_id, "source": source}
    try:
        doc = client.get_document(source, doc_id)
    except (requests.RequestException, ValueError) as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
        return entry
    entry["luceneId"] = doc.get("luceneId")
    entry["bookmarks"] = doc.get("bookmarks", [])
    entry["properties"] = doc.get("properties", {})
    try:
        text, chars, capped = client.get_document_text(source, doc_id)
        entry["text"] = {"content": text, "chars": chars, "capped": capped}
    except (requests.RequestException, ValueError) as exc:
        entry["text"] = {
            "content": None,
            "chars": 0,
            "capped": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return entry


def _doc_total_chars(entry: dict[str, Any]) -> int:
    total = _props_chars(entry.get("properties", {}))
    text = entry.get("text", {})
    if text.get("content") is not None:
        total += text.get("chars", 0)
    return total


def export_bookmark_info(
    bookmark: str,
    dest_dir: str | None,
    source_id: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Exporta as infos estruturadas de um bookmark para um JSON imutável.

    Nome do artefato (M-10): `<UTC-ts>_<source>_<bookmark>.json` — nunca
    sobrescreve exportação anterior (colisão → sufixo `-N`; `overwrite=True`
    é a opt-in explícita de reescrita). `dest_dir` omitido/vazio usa
    `$IPED_MCP_EXPORTS_DIR`. Os docs saem na ORDEM DA ENTRADA do bookmark
    (AC4): o fetch é paralelo, mas o consumo é pela sequência de submissão
    (não mais `as_completed`) — um doc lento não reordena o artefato. O JSON
    é gravado em streaming (uma entry por vez em memória). Falha em
    properties marca o doc como failed (não aborta o lote); falha em text
    mantém o doc com text.error.

    Args:
        bookmark: nome do bookmark.
        dest_dir: pasta de destino (None/vazio = `$IPED_MCP_EXPORTS_DIR`).
        source_id: source no IPED que define o servidor.
        overwrite: True permite reescrever o MESMO arquivo na reexecução.

    Returns:
        Envelope com bookmark, contadores (total_docs/ok/failed/total_chars),
        `json_path` do artefato e `first_failures` (até 5).

    Raises:
        ValueError: bookmark inexistente ou vazio.
    """
    client = get_router()
    groups = client.get_bookmark_docs(bookmark, source_id)
    flat = flatten_groups(groups)
    if not flat:
        raise ValueError(f"Bookmark '{bookmark}' não existe ou está vazio.")
    now = utc_now()
    json_path = export_path(
        dest_dir, source_id, bookmark, ext=".json", now=now, overwrite=overwrite
    )
    generated_at = iso_utc(now)
    return {
        **_stream_bookmark_json(json_path, bookmark, source_id, generated_at, flat),
        "bookmark": bookmark,
        "total_docs": len(flat),
        "json_path": json_path,
    }


def _stream_bookmark_json(
    json_path: str,
    bookmark: str,
    source_id: str,
    generated_at: str,
    flat: list[tuple[str, int]],
) -> dict[str, Any]:
    """Escreve o JSON em streaming (ordem de entrada) e devolve os contadores.

    Args:
        json_path: destino reservado por `export_path`.
        bookmark: nome registrado no cabeçalho do artefato.
        source_id: fonte registrada no cabeçalho do artefato.
        generated_at: ISO-UTC do instante da exportação.
        flat: docs (source, id) na ordem de entrada do bookmark.

    Returns:
        Chaves ok/failed/total_chars/first_failures.
    """
    ok = failed = 0
    total_chars = 0
    first_failures: list[dict[str, Any]] = []
    with open(json_path, "w", encoding="utf-8") as f:
        f.write("{")
        f.write('"bookmark": ')
        json.dump(bookmark, f, ensure_ascii=False)
        f.write(', "source_id": ')
        json.dump(source_id, f, ensure_ascii=False)
        f.write(', "generated_at": ')
        json.dump(generated_at, f, ensure_ascii=False)
        f.write(', "total_docs": ')
        f.write(str(len(flat)))
        f.write(', "docs": [')
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(_fetch_doc, pair) for pair in flat]
            first = True
            for fut in futures:
                entry = fut.result()
                if not first:
                    f.write(",")
                first = False
                json.dump(entry, f, ensure_ascii=False)
                if "error" in entry:
                    failed += 1
                    if len(first_failures) < 5:
                        first_failures.append(
                            {"id": entry["id"], "reason": entry["error"]}
                        )
                else:
                    ok += 1
                total_chars += _doc_total_chars(entry)
        f.write("]")
        f.write(', "ok": ')
        f.write(str(ok))
        f.write(', "failed": ')
        f.write(str(failed))
        if first_failures:
            f.write(', "first_failures": ')
            json.dump(first_failures, f, ensure_ascii=False)
        f.write("}")
    return {
        "ok": ok,
        "failed": failed,
        "total_chars": total_chars,
        "first_failures": first_failures,
    }
