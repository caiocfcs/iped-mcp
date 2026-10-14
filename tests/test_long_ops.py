"""M-9 (DESIGN 09): feedback em operações longas — lotes + envelope de contadores.

Cobre os ACs: AC1 (lotes de scan cobrem o domínio sem sobreposição/perda),
AC2 (contadores nos envelopes das tools longas), AC4 (nenhuma regressão nas
demais chaves — assertivas aditivas) e a política de batch (default 1-shot,
clamp, erros de parâmetro). Backend: FakeWebAPI da suíte (sem rede externa).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from iped_mcp.progress import op_envelope, resolve_batch
from iped_mcp.regex.tool import regex_export_csv, regex_stats, regex_top_values
from iped_mcp.whatsapp.collect import MessageRecord
from iped_mcp.whatsapp import tool as wa_tool
from iped_mcp.bookmarks.download import download_bookmark
from iped_mcp.bookmarks import tool as bm_tool
from tests.conftest import (
    DOC_NO_DISPOSITION_ID,
    DOC_NO_IMAGE_ID,
    DOC_TEXT_ID,
    SOURCE_ID,
    make_doc,
)

ENVELOPE_KEYS = {"phase", "scanned", "processed", "elapsed_s", "remaining_est"}
REGEX_TEST_IDS = [901, 902, 903, 904, 905]


def _add_regex_docs(api: Any) -> None:
    """Registra 5 docs com o detector Regex:TEST (1 valor único por doc)."""
    for doc_id in REGEX_TEST_IDS:
        api.docs[(SOURCE_ID, doc_id)] = make_doc(
            doc_id, f"det_{doc_id}.txt", {"Regex:TEST": [f"v{doc_id}"]}
        )


# ---------------------------------------------------------------- progress


def test_resolve_batch_default_cobre_dominio_inteiro() -> None:
    """Default da tool (1-shot): janela = domínio, sem mais itens."""
    w = resolve_batch(10)
    assert (w.start, w.end, w.scanned, w.has_more) == (0, 10, 10, False)


def test_resolve_batch_rejeita_parametros_invalidos() -> None:
    """offset negativo e limit < 1 falham cedo (contrato das tools)."""
    with pytest.raises(ValueError):
        resolve_batch(10, offset=-1)
    with pytest.raises(ValueError):
        resolve_batch(10, limit=0)


def test_resolve_batch_clampa_fora_do_dominio() -> None:
    """offset além do domínio devolve lote vazio sem 'has_more' fantasma."""
    w = resolve_batch(6, offset=10, limit=3)
    assert (w.start, w.end, w.scanned, w.has_more) == (6, 6, 0, False)


def test_envelope_fase_e_estimativa_em_lote_intermediario() -> None:
    """Lote com domínio pela frente: phase partial + remaining_est proporcional."""
    started = time.monotonic() - 10.0  # lote imaginário de ~10 s
    env = op_envelope(started, scanned=5, processed=2, batch=resolve_batch(10, 0, 5))
    assert env["phase"] == "partial"
    assert env["scanned"] == 5 and env["processed"] == 2
    assert 9.0 < env["remaining_est"] < 11.0  # 10 s varreram metade → ~10 s restantes
    assert env["batch"] == {"offset": 0, "limit": 5, "total": 10, "has_more": True}


def test_envelope_omite_batch_em_tool_sem_fatiamento() -> None:
    """Tools sem lote (coleta whatsapp) devolvem contadores sem chave batch."""
    env = op_envelope(time.monotonic(), scanned=3, processed=3)
    assert set(env) == ENVELOPE_KEYS
    assert env["phase"] == "complete"


# ---------------------------------------------------------------- regex


def test_regex_stats_padrao_mantem_contrato_e_ganha_envelope(
    webapi: Any, fake_router: Any
) -> None:
    """AC4/AC2: chamada default preserva as chaves antigas e soma o envelope."""
    result = regex_stats(SOURCE_ID, "EMAIL")
    assert result["total_docs"] == 1
    assert result["total_matches"] == 1
    assert ENVELOPE_KEYS <= set(result)
    assert result["phase"] == "complete"
    assert result["scanned"] == 1 and result["processed"] == 1
    assert result["batch"] == {"offset": 0, "limit": None, "total": 1, "has_more": False}


def test_regex_lotes_cobrem_dominio_sem_sobreposicao_ou_perda(
    webapi: Any, fake_router: Any
) -> None:
    """AC1: N invocadas com offset/limit cobrem o domínio todo (sem overlap/perda)."""
    _add_regex_docs(webapi)
    seen: list[int] = []
    offsets = [0, 2, 4]
    for i, off in enumerate(offsets):
        result = regex_top_values(SOURCE_ID, "TEST", top=100, offset=off, limit=2)
        assert result["total_docs"] == len(REGEX_TEST_IDS)
        assert result["scanned"] == len(REGEX_TEST_IDS[off : off + 2])  # ultimo lote e parcial
        assert result["batch"]["has_more"] == (i < len(offsets) - 1)
        assert result["phase"] == ("partial" if i < len(offsets) - 1 else "complete")
        seen += [d["id"] for entry in result["top"] for d in entry["sample_docs"]]
    assert sorted(seen) == REGEX_TEST_IDS  # 5 varridos, nenhum repetido, nenhum perdido


def test_regex_batch_rejeita_parametros_invalidos(webapi: Any, fake_router: Any) -> None:
    """A tool propaga o contrato de parâmetro do resolve_batch."""
    with pytest.raises(ValueError):
        regex_stats(SOURCE_ID, "EMAIL", limit=0)
    with pytest.raises(ValueError):
        regex_stats(SOURCE_ID, "EMAIL", offset=-1)


def test_regex_export_csv_lote_marca_janela_no_arquivo(
    webapi: Any, fake_router: Any, tmp_path: Any
) -> None:
    """CSV de lote: só as linhas do lote + metadado `# batch:` para custódia."""
    _add_regex_docs(webapi)
    result = regex_export_csv(
        SOURCE_ID, "TEST", output_dir=str(tmp_path), offset=0, limit=2
    )
    assert result["rows"] == 2
    assert result["phase"] == "partial" and result["batch"]["has_more"] is True
    header = open(result["path"], encoding="utf-8").read()
    assert "# batch: offset=0 limit=2 has_more=true" in header
    full = regex_export_csv(SOURCE_ID, "TEST", output_dir=str(tmp_path))
    full_header = open(full["path"], encoding="utf-8").read()
    assert "# batch:" not in full_header  # domínio inteiro = formato antigo
    assert full["rows"] == 5


# ---------------------------------------------------------------- download


def _add_download_docs(api: Any, ids: list[int]) -> None:
    """Registra docs extras no fake (o /content serve JPEG_BODY a qualquer doc)."""
    for doc_id in ids:
        api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, f"extra_{doc_id}.bin", {})


def test_download_bookmark_envelope_completo(webapi: Any, fake_router: Any, tmp_path: Any) -> None:
    """AC2: download de bookmark inteiro reportou sempre os contadores do lote."""
    webapi.bookmarks["dl3"] = [{"source": SOURCE_ID, "ids": [DOC_NO_IMAGE_ID, DOC_TEXT_ID, DOC_NO_DISPOSITION_ID]}]
    result = download_bookmark("dl3", str(tmp_path), SOURCE_ID)
    assert result["saved"] == 3 and result["failed"] == 0
    assert ENVELOPE_KEYS <= set(result)
    assert result["phase"] == "complete"
    assert result["scanned"] == 3 and result["processed"] == 3
    assert result["batch"] == {"offset": 0, "limit": None, "total": 3, "has_more": False}
    assert len(list(tmp_path.iterdir())) == 3


def test_download_bookmark_lotes_cobrem_dominio(
    webapi: Any, fake_router: Any, tmp_path: Any
) -> None:
    """AC1 análogo: dois lotes salvam os 6 docs e o segundo fecha o domínio."""
    _add_download_docs(webapi, [901, 902, 903])
    ids = [DOC_NO_IMAGE_ID, DOC_TEXT_ID, DOC_NO_DISPOSITION_ID, 901, 902, 903]
    webapi.bookmarks["dl6"] = [{"source": SOURCE_ID, "ids": ids}]
    first = download_bookmark("dl6", str(tmp_path), SOURCE_ID, offset=0, limit=3)
    second = download_bookmark("dl6", str(tmp_path), SOURCE_ID, offset=3, limit=3)
    assert first["phase"] == "partial" and first["batch"]["has_more"] is True
    assert second["phase"] == "complete"
    assert first["scanned"] + second["scanned"] == 6
    assert first["saved"] + second["saved"] == 6  # sem sobreposição: 6 arquivos
    assert len(list(tmp_path.iterdir())) == 6


def test_download_bookmark_offset_fora_do_dominio(
    webapi: Any, fake_router: Any, tmp_path: Any
) -> None:
    """offset além do bookmark não baixa nada e fecha a operação (sem travar)."""
    webapi.bookmarks["dl3"] = [{"source": SOURCE_ID, "ids": [DOC_NO_IMAGE_ID]}]
    result = download_bookmark("dl3", str(tmp_path), SOURCE_ID, offset=10, limit=3)
    assert result["scanned"] == 0 and result["saved"] == 0
    assert result["phase"] == "complete" and result["batch"]["has_more"] is False


def test_tool_bookmarks_download_expoe_lote_sem_sobreposicao(
    webapi: Any, fake_router: Any, tmp_path: Any
) -> None:
    """Follow-up M-9: a tool expõe offset/limit — 2 lotes cobrem o domínio."""
    _add_download_docs(webapi, [911, 912, 913])
    ids = [DOC_NO_IMAGE_ID, DOC_TEXT_ID, DOC_NO_DISPOSITION_ID, 911, 912, 913]
    webapi.bookmarks["dl6t"] = [{"source": SOURCE_ID, "ids": ids}]
    first = bm_tool.bookmarks_download("dl6t", str(tmp_path), SOURCE_ID, limit=4)
    second = bm_tool.bookmarks_download(
        "dl6t", str(tmp_path), SOURCE_ID, offset=4, limit=4
    )
    assert first["phase"] == "partial"
    assert first["batch"] == {"offset": 0, "limit": 4, "total": 6, "has_more": True}
    assert second["phase"] == "complete" and second["batch"]["has_more"] is False
    assert first["scanned"] == 4 and second["scanned"] == 2
    assert first["saved"] + second["saved"] == 6
    assert len(list(tmp_path.iterdir())) == 6  # sem sobreposição: 6 arquivos


def test_tool_bookmarks_download_default_continua_1_shot(
    webapi: Any, fake_router: Any, tmp_path: Any
) -> None:
    """Regressão do default: sem offset/limit a tool baixa tudo numa chamada."""
    webapi.bookmarks["dl3t"] = [
        {"source": SOURCE_ID, "ids": [DOC_NO_IMAGE_ID, DOC_TEXT_ID, DOC_NO_DISPOSITION_ID]}
    ]
    result = bm_tool.bookmarks_download("dl3t", str(tmp_path), SOURCE_ID)
    assert result["phase"] == "complete"
    assert result["batch"] == {"offset": 0, "limit": None, "total": 3, "has_more": False}
    assert result["saved"] == 3


def test_tool_bookmarks_download_valida_parametros_de_lote(
    webapi: Any, fake_router: Any, tmp_path: Any
) -> None:
    """Contrato resolve_batch propagado pela tool: offset >= 0, limit >= 1."""
    webapi.bookmarks["dl3t"] = [{"source": SOURCE_ID, "ids": [DOC_NO_IMAGE_ID]}]
    with pytest.raises(ValueError):
        bm_tool.bookmarks_download("dl3t", str(tmp_path), SOURCE_ID, offset=-1)
    with pytest.raises(ValueError):
        bm_tool.bookmarks_download("dl3t", str(tmp_path), SOURCE_ID, limit=0)


# ---------------------------------------------------------------- whatsapp


def test_whatsapp_overview_sem_wa_inclui_envelope(webapi: Any, fake_router: Any) -> None:
    """Caminho curto (0 docs WA) também responde com o envelope (uniformidade)."""
    result = wa_tool.whatsapp_overview(SOURCE_ID)
    assert result["has_whatsapp"] is False
    assert result["phase"] == "complete"
    assert result["scanned"] == 0 and result["processed"] == 0


def _fake_records(n: int = 2) -> list[MessageRecord]:
    return [
        MessageRecord(
            doc_id=10 + i,
            name=f"WhatsApp Message - Fulano {i}.txt",
            key=f"551199999999{i}",
            kind="chat",
            date="2026-01-01T10:00:00Z",
            from_number=f"551199999999{i}",
            to_number="5511999990003",
        )
        for i in range(n)
    ]


def test_whatsapp_overview_envelope_contabiliza_coleta(
    webapi: Any, fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scanned = docs lidos na coleta (records + dropped); processed = records."""
    records = _fake_records(2)

    def fake_search_ids(query: str, source_id: str, hint: str) -> list[int]:
        return [1, 2, 3] if query == "accountType:WhatsApp" else [1, 2]

    monkeypatch.setattr(wa_tool, "search_ids", fake_search_ids)
    monkeypatch.setattr(wa_tool, "collect_messages", lambda sid: (records, {}, 1))
    monkeypatch.setattr(wa_tool, "_owner_info", lambda sid: None)
    result = wa_tool.whatsapp_overview(SOURCE_ID)
    assert result["has_whatsapp"] is True and result["total_messages"] == 2
    assert result["scanned"] == 3 and result["processed"] == 2  # 2 ok + 1 dropped
    assert result["phase"] == "complete" and "batch" not in result
    assert result["elapsed_s"] >= 0.0


def test_whatsapp_conversation_found_false_tem_envelope(
    webapi: Any, fake_router: Any
) -> None:
    """AC2/AC4: contrato antigo (found/hint) intacto + envelope da coleta vazia."""
    result = wa_tool.whatsapp_conversation(SOURCE_ID, "5511999999999")
    assert result["found"] is False
    assert "hint" in result
    assert result["scanned"] == 0 and result["processed"] == 0
    assert result["phase"] == "complete"
