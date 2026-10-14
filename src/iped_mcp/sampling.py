"""Amostragem estratificada compartilhada (follow-up DRY dos itens M-7/M-9).

Unifica as antigas cópias locais de `regex/scan.py` e `ufed/tool.py`:
começar a lista sem amostragem estratificada pode ver só um tipo de
arquivo e esconder valores presentes no resto do caso (descoberta por
evidência enviesada). Uma única implementação; os dois consumidores
importam daqui.
"""

from __future__ import annotations


def stratified_sample(ids: list[int], n: int) -> list[int]:
    """Escolhe n ids espaçados ao longo da lista (não só os primeiros).

    Args:
        ids: Ids de todos os hits (ordem da busca preservada).
        n: Tamanho desejado da amostra (>= 1 quando `ids` não é vazio).

    Returns:
        Até n ids espaçados uniformemente (todos, se len(ids) <= n).

    Raises:
        ZeroDivisionError: `ids` não vazio com n == 0 (semântica histórica
            das cópias unificadas; os chamadores validam n).
    """
    if len(ids) <= n:
        return list(ids)
    step = len(ids) / n
    return [ids[int(i * step)] for i in range(n)]
