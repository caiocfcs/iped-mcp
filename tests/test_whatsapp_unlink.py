"""M-19: telemetria de desvinculação mídia↔mensagem no WhatsApp (risco nulo).

Pinos da fase:

- contadores de desvinculação (`linkedItems "sha-256:..." ⇄ ufed:SHA256`)
  corretos para mídia resolvida/não resolvida no export;
- acima do limiar `_UNLINK_WARNING_RATIO` o resultado GANHA `warnings`;
  no limiar/abaixo, nada muda (sem aviso);
- nenhuma exceção nova: o export continua sendo escrito com taxa alta;
- `unclassified_messages` (premissa de classificação regex do `name`) no
  envelope compartilhado das tools de coleta.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from iped_mcp.whatsapp import tool as wa_tool
from iped_mcp.whatsapp.collect import MessageRecord
from tests.conftest import SOURCE_ID

SOURCE = "pessoa_teste_9@gmail.example.invalid"
NUM = "5511999999999"


def _rec(doc_id: int, hashes: list[str], kind: str = "chat") -> MessageRecord:
    return MessageRecord(
        doc_id=doc_id,
        name=f"msg_{doc_id}.db",
        key=NUM if kind == "chat" else None,
        kind=kind,
        date="2024-05-01T10:00:00Z",
        from_number=None,
        to_number=None,
        linked_hashes=list(hashes),
    )


def _entry(status: str = "ok") -> dict[str, Any]:
    """Entrada de mídia no formato de `_fetch_media`."""
    return {
        "transcription": None,
        "attachment_status": status,
        "attachment_text": "pdf",
        "attachment_capped": False,
        "attachment_bytes": 10,
    }


# --- contadores (unidade) ------------------------------------------------------


def test_contadores_midia_resolvida_e_perdida() -> None:
    selected = [_rec(1, ["h1", "h2"]), _rec(2, ["h3"]), _rec(3, [])]
    counters = wa_tool._unlink_counters(selected, {"h1": _entry()})
    assert counters == {
        "media_links_total": 3,
        "media_links_unresolved": 2,
        "messages_with_unresolved_media": 2,
    }


def test_contadores_tudo_resolvido() -> None:
    selected = [_rec(1, ["h1", "h2"])]
    counters = wa_tool._unlink_counters(selected, {"h1": _entry(), "h2": _entry()})
    assert counters["media_links_unresolved"] == 0
    assert counters["messages_with_unresolved_media"] == 0


# --- limiar (unidade) -----------------------------------------------------------


def test_aviso_acima_do_limiar() -> None:
    counters = wa_tool._unlink_counters([_rec(1, ["h1", "h2", "h3"])], {})
    notice = wa_tool._unlink_warning(counters)
    assert notice is not None
    assert "3/3" in notice and "ufed:SHA256" in notice


def test_sem_aviso_no_limiar_ou_abaixo() -> None:
    half = wa_tool._unlink_counters([_rec(1, ["h1", "h2"])], {"h1": _entry()})
    assert half["media_links_unresolved"] / half["media_links_total"] == 0.5
    assert wa_tool._unlink_warning(half) is None  # limiar exato nao avisa
    low = wa_tool._unlink_counters(
        [_rec(1, ["h1", "h2", "h3", "h4"])],
        {"h1": _entry(), "h2": _entry(), "h3": _entry()},
    )
    assert wa_tool._unlink_warning(low) is None


def test_sem_links_nenhum_aviso() -> None:
    assert wa_tool._unlink_warning(wa_tool._unlink_counters([], {})) is None


# --- export ponta a ponta (com o limiar ultrapassado) ---------------------------


def _patch_collect(
    monkeypatch: pytest.MonkeyPatch,
    records: list[MessageRecord],
    media: dict[str, dict[str, Any]],
) -> None:
    monkeypatch.setattr(
        wa_tool, "collect_messages", lambda _sid: (records, {}, 0)
    )
    monkeypatch.setattr(
        wa_tool,
        "fetch_docs_parallel",
        lambda _sid, ids: [(i, {}) for i in ids],
    )
    monkeypatch.setattr(wa_tool, "_fetch_media", lambda _sid, _hs: media)


def test_export_acima_do_limiar_acrescenta_aviso(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    records = [_rec(1, ["h1", "h2"]), _rec(2, ["h3"])]
    _patch_collect(monkeypatch, records, {"h1": _entry()})
    out = wa_tool.whatsapp_export_conversation(
        SOURCE, NUM, output_dir=str(tmp_path)
    )
    assert out["media_links_total"] == 3
    assert out["media_links_unresolved"] == 2
    assert out["messages_with_unresolved_media"] == 2
    assert out["warnings"] and "2/3" in out["warnings"][0]
    # risco nulo: o artefato continua sendo escrito normalmente
    assert out["exported"] == 2 and out["path"].endswith(".json")


def test_export_abaixo_do_limiar_sem_aviso(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    records = [_rec(1, ["h1", "h2"]), _rec(2, ["h3"])]
    media = {"h1": _entry(), "h2": _entry()}
    _patch_collect(monkeypatch, records, media)
    out = wa_tool.whatsapp_export_conversation(
        SOURCE, NUM, output_dir=str(tmp_path)
    )
    assert out["media_links_unresolved"] == 1
    assert "warnings" not in out


def test_export_sem_links_mantem_shape_atual(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _patch_collect(monkeypatch, [_rec(1, [])], {})
    out = wa_tool.whatsapp_export_conversation(
        SOURCE, NUM, output_dir=str(tmp_path)
    )
    assert out["media_links_total"] == 0
    assert "warnings" not in out and "warning" not in out


# --- premissa de classificacao no envelope das tools de coleta -------------------


def test_envelope_conta_mensagens_nao_classificadas() -> None:
    records = [_rec(1, []), _rec(2, [], kind="other"), _rec(3, [], kind="group")]
    envelope = wa_tool._collect_envelope(time.monotonic(), records, 0)
    assert envelope["unclassified_messages"] == 1
    assert envelope["scanned"] == 3 and envelope["processed"] == 3


def test_overview_expoe_contador_no_envelope(
    fake_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fonte sem WhatsApp: envelope chega mesmo assim, com contador 0 (dado)."""
    out = wa_tool.whatsapp_overview(SOURCE_ID)
    assert out["unclassified_messages"] == 0
