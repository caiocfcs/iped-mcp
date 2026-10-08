"""Download de arquivos de um bookmark (streaming, threads)."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from iped_mcp.config import WORKERS
from iped_mcp.download import resolve_filename
from iped_mcp.router import get_router

_CHUNK_SIZE = 1024 * 1024  # 1 MB


def _first(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def flatten_groups(groups: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """[{source, ids}] → lista plana (source, id) preservando a ordem dos grupos."""
    flat: list[tuple[str, int]] = []
    for group in groups:
        source = group.get("source")
        for doc_id in group.get("ids", []):
            flat.append((source, doc_id))
    return flat


def download_bookmark(bookmark: str, dest_dir: str, source_id: str) -> dict[str, Any]:
    """Baixa todos os arquivos de um bookmark para dest_dir (streaming, threads).

    Doc sem arquivo (body vazio) → skipped. Falha em 1 doc não aborta o lote;
    segue e reporta em failed (first_failures com até 5).
    """
    client = get_router()
    groups = client.get_bookmark_docs(bookmark, source_id)
    flat = flatten_groups(groups)
    used_names: set[str] = set()
    lock = threading.Lock()

    def _download(pair: tuple[str, int]) -> tuple[str, int, str, int]:
        """→ (status, doc_id, reason, bytes_written)."""
        source, doc_id = pair
        try:
            header_filename, resp = client.get_document_content(source, doc_id)
        except (requests.RequestException, ValueError) as exc:
            return "failed", doc_id, str(exc), 0
        try:
            with resp:
                if resp.headers.get("Content-Length") == "0":
                    return "skipped", doc_id, "", 0
                doc_name = None
                if not header_filename:
                    try:
                        props = client.get_document(source, doc_id).get("properties", {})
                        doc_name = _first(props.get("name"))
                    except (requests.RequestException, ValueError):
                        doc_name = None
                with lock:
                    filename = resolve_filename(doc_id, header_filename, doc_name, used_names)
                path = os.path.join(dest_dir, filename)
                bytes_written = 0
                with open(path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            bytes_written += len(chunk)
                if bytes_written == 0:
                    os.remove(path)
                    return "skipped", doc_id, "", 0
                return "saved", doc_id, "", bytes_written
        except (requests.RequestException, OSError) as exc:
            return "failed", doc_id, str(exc), 0

    saved = skipped = failed = total_bytes = 0
    first_failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for status, doc_id, reason, bytes_written in pool.map(_download, flat):
            if status == "saved":
                saved += 1
                total_bytes += bytes_written
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
                if len(first_failures) < 5:
                    first_failures.append({"id": doc_id, "reason": reason})

    return {
        "saved": saved,
        "skipped": skipped,
        "failed": failed,
        "total_bytes": total_bytes,
        "dest_dir": dest_dir,
        "first_failures": first_failures,
    }
