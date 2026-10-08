"""Leitura de arquivo txt com doc IDs e/ou hashes MD5 (1 por linha)."""

from __future__ import annotations

import re

_MD5_RE = re.compile(r"[0-9a-fA-F]{32}")


def read_ids_or_hashes_file(path: str) -> tuple[list[int], list[str], int]:
    """Lê o txt e devolve (ids, md5s, invalid).

    Cada linha não vazia é classificada:
    - 32 caracteres hex (case-insensitive) -> MD5 (normalizado p/ minúsculo);
    - apenas dígitos -> doc ID (int);
    - demais -> contada em `invalid`.

    O teste de MD5 vem antes do de ID: um número de 32 dígitos é tratado como
    MD5 (doc ID tão grande não existe na prática). Linhas vazias são ignoradas;
    ids e md5s são deduplicados preservando a ordem. Arquivo
    inexistente/ilegível -> ValueError com o path.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(f"Não foi possível ler o arquivo '{path}': {exc}") from exc
    ids: list[int] = []
    md5s: list[str] = []
    seen_ids: set[int] = set()
    seen_md5s: set[str] = set()
    invalid = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _MD5_RE.fullmatch(stripped):
            md5 = stripped.lower()
            if md5 not in seen_md5s:
                seen_md5s.add(md5)
                md5s.append(md5)
            continue
        if stripped.isdigit():
            doc_id = int(stripped)
            if doc_id not in seen_ids:
                seen_ids.add(doc_id)
                ids.append(doc_id)
            continue
        invalid += 1
    return ids, md5s, invalid
