"""Helpers compartilhados de download de arquivos do IPED."""

from __future__ import annotations

import os


def resolve_filename(
    doc_id: int,
    header_filename: str,
    doc_name: str | None,
    used_names: set[str],
) -> str:
    """Escolhe o filename: header → prop name → doc_{id}; colisão → {id}_{name}.

    Atualiza used_names.
    """
    base = header_filename or doc_name or f"doc_{doc_id}"
    base = os.path.basename(base) or f"doc_{doc_id}"
    candidate = base
    if candidate in used_names:
        candidate = f"{doc_id}_{base}"
        n = 1
        while candidate in used_names:
            candidate = f"{doc_id}_{n}_{base}"
            n += 1
    used_names.add(candidate)
    return candidate
