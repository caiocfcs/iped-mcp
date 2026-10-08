"""Exportação de informações estruturadas de um bookmark (JSON)."""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import requests

from iped_mcp.bookmarks.download import flatten_groups
from iped_mcp.config import WORKERS
from iped_mcp.router import get_router

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(name: str) -> str:
    """Nome seguro para filename: substitui chars inválidos por _.

    Resultado vazio ou só underscores → "bookmark".
    """
    cleaned = _UNSAFE_CHARS.sub("_", name).strip()
    return cleaned if cleaned.strip("_") else "bookmark"


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


def export_bookmark_info(bookmark: str, dest_dir: str, source_id: str) -> dict[str, Any]:
    """Exporta as infos estruturadas de um bookmark para <dest_dir>/<bookmark>.json.

    1 doc = 2 requests (properties + text), em paralelo. O JSON é gravado em
    streaming (cada entry vai para o disco assim que completa) — memória
    limitada a ~1 entry, mesmo em bookmarks com texto gigante. Falha em
    properties marca o doc como failed (não aborta o lote); falha em text
    mantém o doc com text.error.
    """
    client = get_router()
    groups = client.get_bookmark_docs(bookmark, source_id)
    flat = flatten_groups(groups)
    if not flat:
        raise ValueError(f"Bookmark '{bookmark}' não existe ou está vazio.")
    json_path = os.path.join(dest_dir, f"{safe_name(bookmark)}.json")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
            for fut in as_completed(futures):
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
        "bookmark": bookmark,
        "total_docs": len(flat),
        "ok": ok,
        "failed": failed,
        "total_chars": total_chars,
        "json_path": json_path,
        "first_failures": first_failures,
    }
