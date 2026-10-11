"""Smoke OPCIONAL contra a WebAPI real -- GET-only, opt-in via IPED_URL.

Sem ``IPED_URL`` no ambiente todos os testes dao skip (AC: a suíte roda
100% offline). Com ``IPED_URL`` definida, o smoke exercita o contrato
basico (GET /sources/) contra o jar em execucao; falha aqui pode ser
rede/ambiente, entao o resultado runtime e informativo, nao um gate.

Blindagem de contexto: nunca imprimir corpo de resposta -- so tipos,
contagens e ids estruturais. Somente metodos GET sao emitidos.
"""

from __future__ import annotations

import os

import pytest

from iped_mcp.client import IPEDClient

RUNTIME_URL_ENV = "IPED_URL"
# Busca match-all no jar real já mediu >10 s na FASE 2d (298 KB de ids).
RUNTIME_TIMEOUT_S = 30.0

pytestmark = pytest.mark.runtime


@pytest.fixture()
def runtime_client() -> IPEDClient:
    """IPEDClient contra a URL real, ou skip quando IPED_URL esta ausente.

    Returns:
        Cliente apontado para $IPED_URL com timeout curto.

    Raises:
        pytest.skip: quando a variavel nao esta definida (skip nao-falha).
    """
    url = os.environ.get(RUNTIME_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"{RUNTIME_URL_ENV} nao definida; smoke runtime e opt-in")
    return IPEDClient(base_url=url, timeout=RUNTIME_TIMEOUT_S)


def test_runtime_sources_envelope_contract(runtime_client: IPEDClient) -> None:
    """GET /sources/ real: envelope {"data"} com itens {id, path} (WI-1/WI-12)."""
    sources = runtime_client.list_sources()
    assert isinstance(sources, list) and sources, "esperado ao menos 1 source"
    assert all({"id", "path"} <= set(s) for s in sources)


def test_runtime_search_groups_contract(runtime_client: IPEDClient) -> None:
    """GET /search real p/ o 1o source: grupos [{source, ids}] sem "total" (WI-2/WI-20)."""
    first_id = runtime_client.list_sources()[0]["id"]
    groups = runtime_client.search("*", source_id=first_id, timeout=RUNTIME_TIMEOUT_S)
    assert isinstance(groups, list)
    for group in groups:
        assert set(group) == {"source", "ids"}
        assert all(isinstance(doc_id, int) for doc_id in group["ids"])
