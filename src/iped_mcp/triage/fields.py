"""Leitura de propriedades no envelope do jar (família triage).

Valores de propriedades no ``GET /docs/{id}`` são SEMPRE lista (WI-1/P12,
``tests/conftest.py::make_doc``); este helper centraliza o ``safe_first``
portado do ICARO para os módulos de execução da família.
"""

from __future__ import annotations

from typing import Any


def first_str(props: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Primeiro elemento (como string) da primeira propriedade não vazia.

    Args:
        props: propriedades do doc no envelope do jar (valores em lista).
        keys: chaves na ordem de fallback (mesma semântica ``a or b`` do
            ICARO, telephony_strategy.py:59/:77-78/:118).

    Returns:
        ``str`` do primeiro valor não vazio; ``""`` quando nada casa.
    """
    for key in keys:
        values = props.get(key) or []
        if values and str(values[0]).strip():
            return str(values[0])
    return ""
