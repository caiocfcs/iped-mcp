"""Montagem e validação de queries de atributos `Regex:*`."""

from __future__ import annotations

import re

from iped_mcp.lucene import quote_value

# Tipos conhecidos (docs validados + sondagem). Lista de referência para
# avisos; a validação dura de verdade é a descoberta via regex_list_types.
KNOWN_TYPES = [
    "BR_CNPJ",
    "BR_CPF",
    "BR_CAR_PLATE",
    "BR_BANK_ACCOUNT",
    "BR_PISPASEP",
    "URL",
    "EMAIL",
    "PHONE",
    "MONEY",
]

_BR_TYPES = {"BR_CNPJ", "BR_CPF", "BR_PISPASEP", "BR_BANK_ACCOUNT"}


def normalize_attribute(attribute: str) -> str:
    """Aceita o tipo com ou sem o prefixo `Regex:` (case preservado)."""
    prefix = "Regex:"
    if attribute.startswith(prefix):
        return attribute[len(prefix) :]
    return attribute


def build_regex_query(attribute: str, value: str | None = None) -> str:
    """Monta a query `Regex:` com o escape `Regex\\:`.

    - sem valor -> `Regex\\:TIPO:*` (todos os docs onde o detector casou)
    - com valor -> `Regex\\:TIPO:valor` (aspas quando há caracteres especiais)
    """
    if value is None:
        return f"Regex\\:{attribute}:*"
    return f"Regex\\:{attribute}:{quote_value(value)}"


def normalize_value(value: str, attribute: str) -> str:
    """Normaliza o valor para contagem de distinct.

    Tipos `BR_` perdem pontuação/brancos (mesmo dado com/sem formatação
    conta uma vez); demais tipos voltam inalterados.
    """
    if attribute in _BR_TYPES:
        return re.sub(r"[^a-z0-9]", "", value.lower())
    return value
