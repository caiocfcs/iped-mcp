"""Montagem e referência de queries de atributos `ufed:`."""

from __future__ import annotations

from iped_mcp.lucene import quote_value

UFED_PREFIX = "ufed:"
# Forma escapada da chave em query Lucene (o separador campo/valor leva "\:").
UFED_ESCAPED_PREFIX = "ufed\\:"

# Lista de referência de campos conhecidos no jar — ADVERTÊNCIA, não validação
# (M-7/DESIGN 07). Runtime 2d 16/28/29: o jar NUNCA retorna erro para campo
# fora da lista — devolve hits (local_path é indexado e estava "negado" aqui)
# ou 0 hits mudo para campo inexistente. A lista real do índice é desconhecida
# e varia por build; a descoberta por evidência é ufed_list_fields(source_id=).
KNOWN_FIELDS = [
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


def normalize_attribute(attribute: str) -> str:
    """Aceita o campo com ou sem o prefixo `ufed:`.

    Args:
        attribute: Nome do campo, com ou sem o prefixo `ufed:`.

    Returns:
        O nome do campo sem prefixo.
    """
    if attribute.startswith(UFED_PREFIX):
        return attribute[len(UFED_PREFIX) :]
    return attribute


def field_advisory(attribute: str) -> str | None:
    """Advertência para campo fora da lista conhecida (nunca recusa).

    Args:
        attribute: Campo `ufed:` já normalizado (sem prefixo).

    Returns:
        Texto de aviso se o campo não está em `KNOWN_FIELDS`; None se é
        campo conhecido (zero regressão para a lista base).
    """
    if attribute in KNOWN_FIELDS:
        return None
    return (
        f"Campo '{UFED_PREFIX}{attribute}' está fora da lista conhecida de "
        "campos; a busca foi executada mesmo assim — o índice pode ou não ter "
        "este campo (use ufed_list_fields(source_id=...) para conferir)."
    )


def zero_hits_advisory(attribute: str) -> str | None:
    """Advertência de falso negativo mudo: 0 hits em campo fora da lista.

    Args:
        attribute: Campo `ufed:` já normalizado (sem prefixo).

    Returns:
        Texto de aviso se o campo é desconhecido; None se é conhecido
        (0 hits em campo conhecido é resposta legítima, sem aviso).
    """
    if attribute in KNOWN_FIELDS:
        return None
    return (
        f"0 resultados para '{UFED_PREFIX}{attribute}': o campo pode não "
        "existir neste índice — o IPED responde 0 hits em silêncio para "
        "campos inexistentes."
    )


def observed_field_names(properties: dict) -> list[str]:
    """Nomes de campos `ufed:` observados nas properties de um documento.

    Args:
        properties: Dicionário de propriedades do doc (chaves cruas do jar).

    Returns:
        Nomes (sem o prefixo `ufed:`) das chaves `ufed:<campo>` não vazias,
        ordenados.
    """
    return sorted(
        key[len(UFED_PREFIX) :]
        for key in properties
        if key.startswith(UFED_PREFIX) and len(key) > len(UFED_PREFIX)
    )


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

    Args:
        attribute: Campo sem prefixo.
        value: Valor único; ausente vira "*".
        values: Lista de valores em OR (alternativa a `value`).
        extra_filters: Cláusulas Lucene extras combinadas com AND.

    Returns:
        A query Lucene montada.

    Raises:
        ValueError: `value` e `values` juntos, ou `values` vazio.
    """
    if value is not None and values is not None:
        raise ValueError("Use 'value' ou 'values', não os dois.")
    if values is not None:
        if not values:
            raise ValueError("'values' não pode ser vazio.")
        clause = (
            f"{UFED_ESCAPED_PREFIX}{attribute}:("
            + " OR ".join(quote_value(v) for v in values)
            + ")"
        )
    else:
        clause = f"{UFED_ESCAPED_PREFIX}{attribute}:{quote_value(value if value is not None else '*')}"
    parts = [clause, *(extra_filters or [])]
    return " AND ".join(parts)


def read_ufed_props(doc: dict, attribute: str) -> object:
    """Lê `properties["ufed:{attribute}"]` (lista -> primeiro valor).

    Args:
        doc: Documento com chave "properties".
        attribute: Campo sem prefixo.

    Returns:
        Primeiro valor quando a propriedade é lista; o valor cru caso
        contrário; None quando ausente ou lista vazia.
    """
    value = doc.get("properties", {}).get(f"{UFED_PREFIX}{attribute}")
    if isinstance(value, list):
        return value[0] if value else None
    return value
