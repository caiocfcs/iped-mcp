"""Follow-up M-7/M-9: helper compartilhado de amostragem (iped_mcp.sampling).

Pina o comportamento idêntico ao das antigas cópias locais (regex/scan.py e
ufed/tool.py) e a identidade do helper usado nos dois consumidores — a
unificação DRY não pode trocar a amostra que as tools descobrem.
"""

from __future__ import annotations

from iped_mcp import sampling
from iped_mcp.regex import scan
from iped_mcp.ufed import tool as ufed_tool


def test_dominio_menor_ou_igual_a_n_devolve_copia_de_tudo() -> None:
    """len(ids) <= n: todos os ids na ordem, em lista nova (sem alias)."""
    ids = [7, 3, 9]
    out = sampling.stratified_sample(ids, 5)
    assert out == [7, 3, 9]
    assert out is not ids
    assert sampling.stratified_sample(ids, 3) == [7, 3, 9]


def test_amostra_tem_n_itens_espacecados_uniformemente() -> None:
    """Semântica histórica: índice int(i * len/n) — n itens sem repetição."""
    ids = list(range(10))
    assert sampling.stratified_sample(ids, 4) == [0, 2, 5, 7]
    assert sampling.stratified_sample(ids, 1) == [0]
    out = sampling.stratified_sample(list(range(1000)), 10)
    assert len(out) == 10 and len(set(out)) == 10
    assert out[0] == 0 and out[-1] < 1000


def test_lista_vazia_devolve_lista_vazia() -> None:
    """Domínio vazio (0 hits) amostra vazio, sem divisão por zero."""
    assert sampling.stratified_sample([], 4) == []


def test_dois_consumidores_usam_a_mesma_implementacao() -> None:
    """DRY: regex/scan e ufed/tool apontam para o MESMO objeto de função."""
    assert scan.stratified_sample is sampling.stratified_sample
    assert ufed_tool.stratified_sample is sampling.stratified_sample
    ids = list(range(23))
    assert scan.stratified_sample(ids, 5) == ufed_tool.stratified_sample(ids, 5)
