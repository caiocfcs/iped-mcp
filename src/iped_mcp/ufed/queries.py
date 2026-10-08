"""Montagem e validação de queries de atributos `ufed:`."""

from __future__ import annotations

from iped_mcp.lucene import quote_value

INDEXED_FIELDS = [
    "EntryName",
    "EntryValue",
    "EntryCategory",
    "Source",
    "extractionName",
    "Name",
    "URL",
    "ChangeTime",
    "decoding_confidence",
    "isrelated",
    "fs",
    "MD5",
    "SHA256",
]

NON_INDEXED_FIELDS = ["local_path", "labels"]


def normalize_attribute(attribute: str) -> str:
    """Aceita o campo com ou sem o prefixo `ufed:`."""
    prefix = "ufed:"
    if attribute.startswith(prefix):
        return attribute[len(prefix) :]
    return attribute


def build_ufed_query(
    attribute: str,
    value: str | None = None,
    values: list[str] | None = None,
    extra_filters: list[str] | None = None,
) -> str:
    """Monta a query `ufed:` com o escape `ufed\\:`.

    - `value` -> `ufed\\:Atributo:valor`
    - `values` -> `ufed\\:Atributo:(v1 OR v2 OR ...)`
    - `extra_filters` -> ` AND ` com cada filtro
    """
    if value is not None and values is not None:
        raise ValueError("Use 'value' ou 'values', não os dois.")
    if values is not None:
        if not values:
            raise ValueError("'values' não pode ser vazio.")
        clause = f"ufed\\:{attribute}:(" + " OR ".join(quote_value(v) for v in values) + ")"
    else:
        clause = f"ufed\\:{attribute}:{quote_value(value if value is not None else '*')}"
    parts = [clause, *(extra_filters or [])]
    return " AND ".join(parts)


def read_ufed_props(doc: dict, attribute: str) -> object:
    """Lê `properties["ufed:{attribute}"]` (lista -> primeiro valor)."""
    value = doc.get("properties", {}).get(f"ufed:{attribute}")
    if isinstance(value, list):
        return value[0] if value else None
    return value
