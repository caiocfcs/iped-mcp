"""Download de arquivos de um bookmark (streaming, threads) + envelope M-9.

O envelope de feedback (progresso/op_envelope) e o fatiamento por
(offset, limit) da lista plana de docs permitem ao agente retomar/limitar
um download de minutos em vez de esperar o bookmark inteiro cego. Desde o
M-10 (DESIGN 10) cada arquivo é gravado pelo helper único
`iped_mcp.download.export_path`: nome `<UTC-ts>_<source>_<nome>`, extensão
pelo Content-Type real (P1-3) e reserva atômica sem sobrescrita.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import requests

from iped_mcp.config import WORKERS
from iped_mcp.download import (
    export_path,
    pick_export_name,
    resolve_export_dir,
    write_stream_to,
)
from iped_mcp.progress import op_envelope, resolve_batch
from iped_mcp.router import get_router

_MAX_FAILURES_REPORTED = 5  # teto legado de first_failures no envelope


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


def _download_one(
    client: Any, dest_dir: str, overwrite: bool, pair: tuple[str, int]
) -> tuple[str, int, str, int]:
    """Baixa um (source, doc_id) → (status, doc_id, reason, bytes_written).

    Nunca levanta: doc sem arquivo (body vazio) → skipped; erro de rede ou
    disco → failed com motivo (o chamador acumula em first_failures). A
    reserva do nome é do helper único (M-10): colisão intra-lote, entre
    lotes ou entre reexecuções ganha sufixo `-N` em vez de sobrescrever.
    """
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
            base = pick_export_name(doc_id, header_filename, doc_name)
            path = export_path(
                dest_dir,
                source,
                base,
                content_type=resp.headers.get("Content-Type"),
                overwrite=overwrite,
            )
            bytes_written = write_stream_to(path, resp)
            if bytes_written == 0:
                os.remove(path)
                return "skipped", doc_id, "", 0
            return "saved", doc_id, "", bytes_written
    except (requests.RequestException, OSError) as exc:
        return "failed", doc_id, str(exc), 0


@dataclass
class _Outcome:
    """Acumulador de resultados de um lote (vida restrita a _run_downloads)."""

    saved: int = 0
    skipped: int = 0
    failed: int = 0
    total_bytes: int = 0
    first_failures: list[dict[str, Any]] = field(default_factory=list)

    def payload(self, dest_dir: str) -> dict[str, Any]:
        """Chaves legadas do envelope de download (contrato pré-M-9)."""
        return {
            "saved": self.saved,
            "skipped": self.skipped,
            "failed": self.failed,
            "total_bytes": self.total_bytes,
            "dest_dir": dest_dir,
            "first_failures": list(self.first_failures),
        }


def _reduce(outcome: _Outcome, status: str, doc_id: int, reason: str, written: int) -> None:
    """Acumula um resultado de download no outcome (falha entra até o teto)."""
    if status == "saved":
        outcome.saved += 1
        outcome.total_bytes += written
    elif status == "skipped":
        outcome.skipped += 1
    else:
        outcome.failed += 1
        if len(outcome.first_failures) < _MAX_FAILURES_REPORTED:
            outcome.first_failures.append({"id": doc_id, "reason": reason})


def _run_downloads(
    client: Any, pairs: list[tuple[str, int]], dest_dir: str, overwrite: bool
) -> _Outcome:
    """Baixa `pairs` em paralelo (WORKERS threads) e acumula no outcome.

    A reserva atômica do `export_path` (M-10) substitui o seed de
    `os.listdir` criado no M-9: nome em uso nunca mais sobrescreve arquivo —
    quem chega primeiro leva o nome-base e os demais ganham sufixo `-N`.
    """
    outcome = _Outcome()

    def _one(pair: tuple[str, int]) -> tuple[str, int, str, int]:
        return _download_one(client, dest_dir, overwrite, pair)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for status, doc_id, reason, written in pool.map(_one, pairs):
            _reduce(outcome, status, doc_id, reason, written)
    return outcome


def download_bookmark(
    bookmark: str,
    dest_dir: str | None,
    source_id: str,
    offset: int = 0,
    limit: int | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Baixa os arquivos de um bookmark (streaming, threads) sem sobrescrever.

    Cada arquivo sai como `<UTC-ts>_<source>_<nome>` (M-10): reexecução do
    mesmo bookmark cria artefatos NOVOS (colisão → `-N`), nunca destrói o
    download anterior; `overwrite=True` é a opt-in explícita de reescrita.
    `dest_dir` omitido/vazio usa `$IPED_MCP_EXPORTS_DIR`. Doc sem arquivo
    (body vazio) → skipped. Falha em 1 doc não aborta o lote; segue e
    reporta em failed (first_failures com até 5). `offset`/`limit` fatiam a
    lista plana de docs do bookmark (ordem dos grupos preservada): o default
    (0, None) baixa tudo como antes; com lote, `batch.has_more` informa o
    que falta e a próxima chamada continua de offset + limit. O envelope
    inclui os contadores da operação (scanned/processed/phase/elapsed_s/
    remaining_est/batch — ver iped_mcp.progress).

    Args:
        bookmark: nome do bookmark.
        dest_dir: pasta de destino (None/vazio = `$IPED_MCP_EXPORTS_DIR`).
        source_id: source no IPED que define o servidor.
        offset: docs a pular antes do lote (padrão 0 = tudo).
        limit: docs no lote (omitido = até o fim).
        overwrite: True permite reescrever os MESMOS arquivos (default False).

    Returns:
        Envelope legado (saved/skipped/failed/total_bytes/dest_dir/
        first_failures) + os contadores do helper único `op_envelope`.
    """
    started_at = time.monotonic()
    client = get_router()
    groups = client.get_bookmark_docs(bookmark, source_id)
    flat = flatten_groups(groups)
    window = resolve_batch(len(flat), offset, limit)
    target = resolve_export_dir(dest_dir)
    outcome = _run_downloads(client, flat[window.start : window.end], target, overwrite)
    envelope = op_envelope(
        started_at, scanned=window.scanned, processed=outcome.saved, batch=window
    )
    return {**outcome.payload(target), **envelope}
