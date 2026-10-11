"""T-2 (DESIGN 09 §3/§5): transcripts_generate — artefato file-backed e seguro p/ contexto.

Casos pinados: com áudios (formato literal do bloco, caminho ">>"→"/",
texto colapsado, dedup por hash e ordenação temporal com/sem data),
contrato zero sem áudios e sem transcrições (T2-2), cap do preview marcado
(T2-3), nome M-10 com colisão sufixo -1 (T2-4), guarda AUDIO_SCAN_MAX_DOCS
marcada (T2-5), ordem de fallback das queries com interrupção na primeira
que produz registros (T2-6) e source inexistente via M-2 (T2-7). Backend:
FakeWebAPI (matcher ``ext:`` estendido p/ o fallback — conftest F2).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from iped_mcp import download
from iped_mcp.triage import knowledge, transcripts
from tests.conftest import SOURCE_ID, UNKNOWN_SOURCE, make_doc

ENVELOPE_KEYS = {
    "source_id",
    "file_path",
    "audio_docs",
    "with_transcript",
    "without_transcript",
    "duplicates",
    "file_bytes",
    "preview",
    "preview_truncated",
    "counters",
    "truncated",
    "warnings",
}
AUDIO_QUERIES_LOGGED = 3  # /search por query candidata tentada


def _add_audio(
    api: Any,
    doc_id: int,
    name: str,
    path: str,
    hash_value: str,
    transcript: str | None,
    created: str = "",
    ext: str | None = None,
    category: str = "Audios",
) -> None:
    """Registra doc de áudio no fake (props no formato do jar: lista).

    Args:
        api: estado observável do FakeWebAPI.
        doc_id: id do documento.
        name: propriedade ``name``.
        path: propriedade ``path`` (com ">>" como no índice).
        hash_value: propriedade ``hash`` (chave de dedup).
        transcript: metadado ``audio:transcription`` (None = sem ASR).
        created: valor de ``criacao`` (vazio = sem data).
        ext: propriedade ``ext`` (fallback por extensão).
        category: categoria do doc (casa com category_hits).
    """
    props: dict[str, list[Any]] = {
        "category": [category],
        "path": [path],
        "hash": [hash_value],
        "criacao": [created],
    }
    if transcript is not None:
        props["audio:transcription"] = [transcript]
    if ext is not None:
        props["ext"] = [ext]
    api.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, name, props)
    api.category_hits.setdefault(category, []).append(doc_id)


def _search_count(api: Any) -> int:
    """Nº de pedidos GET /search no log do fake (tráfego pós-startup)."""
    return sum(1 for e in api.requests_log if e["path"] == "/search")


# --- T2-1: com áudios ---------------------------------------------------------


def test_fluxo_feliz_formato_ordem_dedup(
    webapi: Any, fake_router: Any, tmp_path: Path
) -> None:
    """Blocos literais, caminho normalizado, dedup e ordenação temporal."""
    _add_audio(webapi, 901, "msg1.m4a", "E:evidencia>>msg1.m4a", "AAAA",
               "  fala   1\ncom\nquebras ", created="02/01/2026 10:00:00")
    _add_audio(webapi, 902, "duplicata.m4a", "E:x>>d.m4a", "aaaa", "dup")
    _add_audio(webapi, 903, "sem-asr.opus", "E:x>>s.opus", "CCCC", None)
    _add_audio(webapi, 904, "segundo.wav", "E:y>>segundo.wav", "DDDD",
               "fala dois", created="1767225600000")  # epoch ms: 2026-01-01
    _add_audio(webapi, 905, "terceiro.amr", "E:z>>t.amr", "EEEE", "fala tres")

    out = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))

    assert set(out) == ENVELOPE_KEYS
    assert "message" not in out
    assert (out["audio_docs"], out["with_transcript"],
            out["without_transcript"], out["duplicates"]) == (5, 3, 1, 1)
    assert out["preview_truncated"] is False
    assert out["truncated"] == {"scan_cap_applied": False}
    assert out["warnings"] == []
    assert out["counters"]["docs_fetched"] == 5
    name = os.path.basename(out["file_path"])
    assert re.match(r"^\d{8}T\d{6}Z_" + re.escape(SOURCE_ID) +
                    r"_Transcricoes\.txt$", name)
    content = Path(out["file_path"]).read_text(encoding="utf-8")
    assert content == (
        "Nome: segundo.wav\nCaminho: E:y/segundo.wav\nHASH: DDDD\n\n"
        "fala dois\n"
        + "\n"
        + "Nome: msg1.m4a\nCaminho: E:evidencia/msg1.m4a\nHASH: AAAA\n\n"
        "fala 1 com quebras\n"
        + "\n"
        + "Nome: terceiro.amr\nCaminho: E:z/t.amr\nHASH: EEEE\n\n"
        "fala tres\n"
    )  # com data (epoch 2026-01-01 < 02/01/2026) antes; sem data por último
    assert out["file_bytes"] == len(content.encode("utf-8"))
    assert out["preview"] == content


# --- T2-2: contratos zero -----------------------------------------------------


def test_sem_audios_contrato_zero_sem_arquivo(
    webapi: Any, fake_router: Any, tmp_path: Path
) -> None:
    """Nenhum hit nas 3 queries: file_path=None, contadores 0, message."""
    out = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))

    assert set(out) == ENVELOPE_KEYS | {"message"}
    assert out["file_path"] is None
    assert (out["audio_docs"], out["with_transcript"],
            out["without_transcript"], out["file_bytes"]) == (0, 0, 0, 0)
    assert out["preview"] == "" and out["preview_truncated"] is False
    assert "audio:transcription" in out["message"]
    assert list(tmp_path.iterdir()) == []  # não grava arquivo vazio


def test_audios_sem_transcricao_contrato_zero(
    webapi: Any, fake_router: Any, tmp_path: Path
) -> None:
    """Áudios existem mas nenhum com ASR: mesmo contrato zero nomeado."""
    _add_audio(webapi, 910, "mudo.opus", "E:m>>m.opus", "FFFF", None)

    out = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))

    assert out["file_path"] is None
    assert out["audio_docs"] == 1 and out["without_transcript"] == 1
    assert "audio:transcription" in out["message"]
    assert list(tmp_path.iterdir()) == []


# --- T2-3: cap do preview sempre marcado ---------------------------------------


def test_preview_cap_exato_e_marcado(
    webapi: Any, fake_router: Any, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arquivo maior que PREVIEW_CHARS: preview com o tamanho exato e flag."""
    monkeypatch.setattr(knowledge, "PREVIEW_CHARS", 50)
    _add_audio(webapi, 920, "longo.wav", "E:l>>l.wav", "LLLL", "x" * 300)

    out = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))

    assert len(out["preview"]) == 50
    assert out["preview"].endswith(knowledge.PREVIEW_ELLIPSIS)
    assert out["preview_truncated"] is True


# --- T2-4: nome M-10 com no-clobber --------------------------------------------


def test_colisao_de_nome_gera_sufixo_e_preserva_o_primeiro(
    webapi: Any, fake_router: Any, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duas execuções no mesmo instante: -1 sem tocar o primeiro artefato."""
    monkeypatch.setattr(
        download, "utc_now",
        lambda: datetime(2026, 10, 11, 12, 34, 56, tzinfo=timezone.utc),
    )
    _add_audio(webapi, 930, "a.wav", "E:a>>a.wav", "H1", "primeira")

    first = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))
    _add_audio(webapi, 931, "b.wav", "E:a>>b.wav", "H2", "segunda tambem")
    second = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))

    base = "20261011T123456Z_" + SOURCE_ID + "_Transcricoes"
    assert os.path.basename(first["file_path"]) == base + ".txt"
    assert os.path.basename(second["file_path"]) == base + "-1.txt"
    assert "primeira" in Path(first["file_path"]).read_text(encoding="utf-8")
    assert "segunda" not in Path(first["file_path"]).read_text(encoding="utf-8")


# --- T2-5: guarda AUDIO_SCAN_MAX_DOCS marcada -----------------------------------


def test_guarda_de_varredura_marcada(
    webapi: Any, fake_router: Any, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUDIO_SCAN_MAX_DOCS pequeno limita a varredura com truncamento."""
    monkeypatch.setattr(knowledge, "AUDIO_SCAN_MAX_DOCS", 2)
    for i in range(3):
        _add_audio(webapi, 940 + i, f"a{i}.wav", f"E:g>>a{i}.wav",
                   f"K{i}", f"fala {i}")

    out = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))

    assert out["truncated"]["scan_cap_applied"] is True
    assert out["audio_docs"] == 2


# --- T2-6: ordem de fallback das queries ----------------------------------------


def test_fallback_so_para_quando_query_anterior_nao_produziu(
    webapi: Any, fake_router: Any, tmp_path: Path
) -> None:
    """Só a 3ª query (ext:...) casa: usa-a; 1ª com hits interrompe as demais."""
    _add_audio(webapi, 950, "so-ext.opus", "E:f>>so.opus", "FX", "via ext",
               category="OutraCoisa", ext="opus")

    out = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))

    assert out["with_transcript"] == 1
    assert _search_count(webapi) == AUDIO_QUERIES_LOGGED  # 2 vazias + a que casou

    _add_audio(webapi, 951, "cat.wav", "E:f>>cat.wav", "FY", "via category")
    out2 = transcripts.transcripts_generate(SOURCE_ID, dest_dir=str(tmp_path))
    assert out2["with_transcript"] == 1
    assert _search_count(webapi) == AUDIO_QUERIES_LOGGED + 1  # para na 1ª


# --- T2-7: source inexistente (política M-2) ------------------------------------


def test_source_inexistente_vira_value_error_estavel(fake_router: Any) -> None:
    """ValueError com o texto estável do Router.resolve, antes do HTTP."""
    with pytest.raises(ValueError, match="não encontrado em nenhum endpoint"):
        transcripts.transcripts_generate(UNKNOWN_SOURCE)
