"""R-TRIAGE-1 (DESIGN 09-B §2.5): módulo triage/budget.py e retrofit T-1/T-2.

Casos pinados: mecânica do ``HttpBudget`` (cap exato, ``exceeded`` nunca
levanta exceção — §2.5e), contrato do ``fetch_within_budget`` (quantidade
omitida num ÚNICO elemento — contrato que o R-TRIAGE-4 violation violava
com ``len()``), warning canônico nomeando cap/chamadas, extração sem
mudança de contrato (identity reexporta os mesmos objetos) e o pino de
exatidão/estouro do retrofit nas duas tools piloto T-1/T-2 (mesmo padrão
das ondas B/C: http_calls == tráfego do fake; estouro → partial+warning).
Backend: FakeWebAPI da suíte; nenhum acesso externo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from iped_mcp import progress
from iped_mcp.triage import budget, identity, knowledge, telephony, transcripts
from tests.conftest import SOURCE_ID, make_doc
import tests.test_triage_telephony as t1
import tests.test_triage_transcripts as t2

_SRC = Path("src/iped_mcp/triage")


# --- mecânica do contador (§2.5e) ------------------------------------------


def test_permite_ate_o_cap_exato_e_depois_excede_sem_excecao() -> None:
    """``allows`` é verdadeiro até o cap; estouro é flag, nunca exceção."""
    b = budget.HttpBudget(3)

    assert [b.allows() for _ in range(3)] == [True, True, True]
    for _ in range(3):
        b.record()
    assert b.allows() is False
    b.exceeded = True  # padrão dos fluxos: flag, não raise (§2)
    assert (b.cap, b.used, b.exceeded) == (3, 3, True)


def test_record_aceita_custo_de_lote() -> None:
    """Um ``record(k)`` conta k GETs (lote /docs em paralelo)."""
    b = budget.HttpBudget(10)

    b.record(7)

    assert b.used == 7 and b.allows(3) and not b.allows(4)


# --- contrato do fetch orçado ------------------------------------------------


def test_fetch_orcado_poem_quantidade_em_um_unico_elemento(
    webapi: Any, fake_router: Any
) -> None:
    """5 candidatos com cap 2: omitted == [3] (contrato docs_omitted[0])."""
    for i in range(5):
        webapi.docs[(SOURCE_ID, 600 + i)] = make_doc(600 + i, f"b{i}.txt", {})
    b = budget.HttpBudget(2)
    omitted: list[int] = []

    pairs = budget.fetch_within_budget(SOURCE_ID, list(range(600, 605)), b, omitted)

    assert len(pairs) == 2 and b.used == 2 and b.exceeded is True
    assert omitted == [3]  # quantidade única — base do R-TRIAGE-4


def test_fetch_orcado_sem_estouro_nao_marca_nada(
    webapi: Any, fake_router: Any
) -> None:
    """Cabendo tudo: nenhum GET omitido, nenhuma flag, omitted vazio."""
    webapi.docs[(SOURCE_ID, 610)] = make_doc(610, "ok.txt", {})
    b = budget.HttpBudget(5)
    omitted: list[int] = []

    pairs = budget.fetch_within_budget(SOURCE_ID, [610], b, omitted)

    assert len(pairs) == 1 and b.used == 1 and b.exceeded is False
    assert omitted == [] and budget.budget_warnings(b, omitted) == []


# --- warning canônico ----------------------------------------------------------


def test_warning_nomeia_cap_chamadas_e_omitidos() -> None:
    """Estouro documenta cap, contagem e docs de fora (template §2.3)."""
    b = budget.HttpBudget(4)
    b.used, b.exceeded = 4, True

    (warning,) = budget.budget_warnings(b, [7])

    assert "http_budget_exceeded" in warning
    assert "cap 4" in warning and "4 GETs" in warning and "7 documentos" in warning


def test_warning_sem_omitidos_aponta_as_queries() -> None:
    """Cap tingido no laço de /search: warning nomeia as queries restantes."""
    b = budget.HttpBudget(4)
    b.used, b.exceeded = 4, True

    (warning,) = budget.budget_warnings(b, [])

    assert "demais categorias não consultadas" in warning


# --- extração sem mudança de contrato (R-TRIAGE-1) ---------------------------


def test_identity_reexporta_o_modulo_sem_segunda_copia() -> None:
    """``identity._Budget`` é o ``budget.HttpBudget`` extraído (aliases)."""
    assert identity._Budget is budget.HttpBudget
    assert identity._budget_warnings is budget.budget_warnings
    assert identity._fetch_within_budget is budget.fetch_within_budget
    assert not (
        "class _Budget:" in (_SRC / "identity.py").read_text(encoding="utf-8")
    )  # a implementação vive só em budget.py (regra 3/9)


def test_tool_budgets_cobre_default_com_partida_segura() -> None:
    """Política: default 3000 e T-1/T-2 pilotados no default (medidos)."""
    assert knowledge.DEFAULT_MAX_INTERNAL_GETS == 3000
    assert knowledge.TOOL_BUDGETS["triage_telephony"] == 3000
    assert knowledge.TOOL_BUDGETS["transcripts_generate"] == 3000


# --- retrofit T-1: http_calls no envelope -------------------------------------


def test_t1_http_calls_igual_ao_trafego_do_fake(
    webapi: Any, fake_router: Any
) -> None:
    """Pino de exatidão (mesmo padrão das ondas): 5 buscas + N docs == fake."""
    t1._add_call(webapi, 811, to="(5511999990000)")
    t1._add_agenda_contact(webapi, 812, "Fulano", "(55 11 99999-0000)")
    webapi.requests_log.clear()

    out = telephony.triage_telephony(SOURCE_ID)

    paths = [entry["path"] for entry in webapi.requests_log]
    searches = paths.count("/search")
    docs = sum("/docs/" in p for p in paths)
    assert searches == len(knowledge.TELEPHONY_CATEGORIES) == 5
    assert out["counters"]["http_calls"] == searches + docs == 5 + 2
    assert out["truncated"]["http_budget_exceeded"] is False


def test_t1_orcamento_estourado_vira_parcial_marcado(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cap = 5 buscas + 1 doc: parcial não vazio, warning, nunca exceção."""
    cap = len(knowledge.TELEPHONY_CATEGORIES) + 1
    monkeypatch.setitem(knowledge.TOOL_BUDGETS, "triage_telephony", cap)
    for i in range(3):
        t1._add_call(webapi, 820 + i, to=f"(551199999111{i})")

    out = telephony.triage_telephony(SOURCE_ID)

    counters, truncated = out["counters"], out["truncated"]
    assert counters["phase"] == progress.PHASE_PARTIAL
    assert counters["http_calls"] == cap
    assert truncated["http_budget_exceeded"] is True
    assert truncated["docs_omitted_by_budget"] == 2
    assert any(
        "http_budget_exceeded" in w and f"cap {cap}" in w for w in out["warnings"]
    )
    assert len(out["contacts"]) == 1  # parcial NÃO vazio


# --- retrofit T-2: http_calls no envelope -------------------------------------


def test_t2_http_calls_igual_ao_trafego_do_fake(
    webapi: Any, fake_router: Any, tmp_path: Any
) -> None:
    """Pino de exatidão T-2: 1 busca (1ª query produz) + N docs == fake."""
    t2._add_audio(webapi, 830, "a.wav", "E:a>>a.wav", "HA", "fala um")
    t2._add_audio(webapi, 831, "b.wav", "E:b>>b.wav", "HB", "fala dois")
    webapi.requests_log.clear()

    out = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))

    paths = [entry["path"] for entry in webapi.requests_log]
    searches = paths.count("/search")
    docs = sum("/docs/" in p for p in paths)
    assert searches == 1  # primeira query produz registros e encerra
    assert out["counters"]["http_calls"] == searches + docs == 1 + 2
    assert out["truncated"]["http_budget_exceeded"] is False


def test_t2_orcamento_estourado_vira_parcial_marcado(
    webapi: Any,
    fake_router: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap 3 = 1 busca + 2 docs: artefato parcial, warning, nunca exceção."""
    monkeypatch.setitem(knowledge.TOOL_BUDGETS, "transcripts_generate", 3)
    for i in range(4):
        t2._add_audio(webapi, 840 + i, f"a{i}.wav", f"E:o>>a{i}.wav", f"H{i}", f"f{i}")

    out = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))

    counters, truncated = out["counters"], out["truncated"]
    assert counters["phase"] == progress.PHASE_PARTIAL
    assert counters["http_calls"] == 3
    assert truncated["http_budget_exceeded"] is True
    assert truncated["docs_omitted_by_budget"] == 2
    assert any("http_budget_exceeded" in w and "cap 3" in w for w in out["warnings"])
    assert out["with_transcript"] == 2  # parcial NÃO vazio

