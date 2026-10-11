"""Leitura de arquivo txt com doc IDs e/ou hashes MD5 (1 por linha).

Classificação por linha (M-14 — ordem invertida em relação ao baseline):

1. prefixo explícito ``md5:`` força hash (único jeito de expressar um MD5
   formado só por dígitos, evento com probabilidade ~10^-7);
2. linha só de dígitos é SEMPRE doc ID, qualquer que seja o comprimento —
   doc IDs reais são inteiros pequenos e Sequential no caso do IPED, nunca
   uma cadeia de 32 dígitos;
3. linha com exatamente 32 caracteres hex e ao menos uma letra é MD5
   (doc ID jamais contém letras);
4. o restante é contado como inválido.

O baseline tratava 32-hex como MD5 ANTES de tentar doc ID, classificando
uma linha de 32 dígitos como hash (D-08). A ordem atual remove essa
ambiguidade sem quebrar arquivos existentes: MD5s com letras — a forma
prática de todo MD5 gerado por ferramenta — continuam nuas, sem prefixo.
"""

from __future__ import annotations

import re

_MD5_RE = re.compile(r"[0-9a-fA-F]{32}")
_MD5_PREFIX = "md5:"

# Rótulos internos da classificação (nunca saem do módulo).
_KIND_ID = "id"
_KIND_MD5 = "md5"
_KIND_INVALID = "invalid"


def _classify(stripped: str) -> tuple[str, object] | None:
    """Classifica uma linha já stripada.

    Args:
        stripped: linha sem espaços nas bordas.

    Returns:
        ``("id", int)``, ``("md5", str)``, ``("invalid", None)`` ou ``None``
        para linha vazia (ignorada, não contada como inválida).
    """
    if not stripped:
        return None
    if stripped.lower().startswith(_MD5_PREFIX):
        candidate = stripped[len(_MD5_PREFIX):].strip().lower()
        if _MD5_RE.fullmatch(candidate):
            return (_KIND_MD5, candidate)
        return (_KIND_INVALID, None)
    if stripped.isdigit():
        return (_KIND_ID, int(stripped))
    if _MD5_RE.fullmatch(stripped):
        return (_KIND_MD5, stripped.lower())
    return (_KIND_INVALID, None)


def read_ids_or_hashes_file(path: str) -> tuple[list[int], list[str], int]:
    """Lê o txt e devolve (ids, md5s, invalid).

    Cada linha não vazia é classificada por ``_classify`` (doc ID tem
    precedência sobre MD5; prefixo ``md5:`` força hash). Linhas vazias são
    ignoradas; ids e md5s são deduplicados preservando a ordem. Arquivo
    inexistente/ilegível -> ValueError com o path.

    Args:
        path: caminho do arquivo txt (1 item por linha, UTF-8).

    Returns:
        (doc IDs na ordem de aparição, MD5s normalizados p/ minúsculo,
        contagem de linhas inválidas).

    Raises:
        ValueError: arquivo inexistente ou ilegível.
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
        entry = _classify(line.strip())
        if entry is None:
            continue
        kind, value = entry
        if kind == _KIND_INVALID:
            invalid += 1
        elif kind == _KIND_ID:
            if value not in seen_ids:
                seen_ids.add(value)
                ids.append(value)
        elif kind == _KIND_MD5:
            if value not in seen_md5s:
                seen_md5s.add(value)
                md5s.append(value)
    return ids, md5s, invalid
