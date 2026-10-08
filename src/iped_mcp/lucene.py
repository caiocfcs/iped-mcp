"""Utilitários compartilhados de montagem de queries Lucene."""

from __future__ import annotations

_SPECIAL_CHARS = set(' +-&&|!(){}[]^"~?:\\/')


def quote_value(value: str) -> str:
    """Aspa o valor se contiver caracteres especiais do Lucene."""
    if any(ch in _SPECIAL_CHARS for ch in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value
