"""M-10 (DESIGN 10): exports imutáveis e únicos — UTC, fonte no nome, no-clobber.

Cobre os ACs: AC1 (reexecução não altera o primeiro artefato; colisão vira
sufixo `-N`), AC2 (nome `<UTC-ts>_<source>_<safe_name>`), AC3 (homônimos de
duas fontes não colidem), AC4 (docs do export_info na ordem da entrada),
AC5 (extensão segue o Content-Type real — fixture P1-3 — e timestamps
internos em UTC) — mais o default `IPED_MCP_EXPORTS_DIR` valendo para
core/bookmarks (unificação mínima do M-17). Backend: FakeWebAPI da suíte.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from iped_mcp import config
from iped_mcp.bookmarks import export_info as export_info_mod
from iped_mcp.bookmarks.download import download_bookmark
from iped_mcp.bookmarks.export_info import export_bookmark_info
from iped_mcp.core.tool import doc_export
from iped_mcp.download import (
    export_path,
    extension_for_content_type,
    iso_utc,
    safe_name,
    utc_now,
)
from iped_mcp.regex import tool as regex_tool
from iped_mcp.regex.tool import regex_export_csv
from tests.conftest import DOC_TEXT_ID, EXISTING_BOOKMARK, SOURCE_ID, make_doc

UTC_MOMENT = datetime(2026, 10, 11, 12, 34, 56, tzinfo=timezone.utc)
FILE_TS_RE = re.compile(r"^\d{8}T\d{6}Z_")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REGEX_TEST_IDS = [901, 902, 903, 904, 905]


def _add_regex_docs(api: Any) -> None:
    """Registra 5 docs com o detector Regex:TEST (cópia do helper do M-9)."""
    for doc_id in REGEX_TEST_IDS:
        api.docs[(SOURCE_ID, doc_id)] = make_doc(
            doc_id, f"det_{doc_id}.txt", {"Regex:TEST": [f"v{doc_id}"]}
        )


# ------------------------------------------------------------- export_path


def test_nome_do_export_e_utc_source_nome(tmp_path: Any) -> None:
    """AC2: `<UTC-ts>_<source>_<safe_name><ext>` literal."""
    path = export_path(str(tmp_path), "server-1", "relatorio.pdf", now=UTC_MOMENT)
    assert os.path.basename(path) == "20261011T123456Z_server-1_relatorio.pdf"
    assert os.path.isfile(path)


def test_colisao_gera_sufixo_e_preserva_o_primeiro(tmp_path: Any) -> None:
    """AC1: reexecução no mesmo instante cria `-1` sem tocar o primeiro."""
    p1 = export_path(str(tmp_path), "s", "a.json", ext=".json", now=UTC_MOMENT)
    with open(p1, "w", encoding="utf-8") as f:
        f.write("PRIMEIRO")
    mtime_ns = os.stat(p1).st_mtime_ns
    p2 = export_path(str(tmp_path), "s", "a.json", ext=".json", now=UTC_MOMENT)
    assert p2 != p1
    assert os.path.basename(p2) == "20261011T123456Z_s_a-1.json"
    with open(p1, encoding="utf-8") as f:
        assert f.read() == "PRIMEIRO"
    assert os.stat(p1).st_mtime_ns == mtime_ns


def test_overwrite_explicito_reusa_o_mesmo_arquivo(tmp_path: Any) -> None:
    """Opt-in `overwrite=True`: mesmo nome, sem sufixo (comportamento do design)."""
    p1 = export_path(str(tmp_path), "s", "a.csv", ext=".csv", now=UTC_MOMENT)
    p2 = export_path(str(tmp_path), "s", "a.csv", ext=".csv", now=UTC_MOMENT, overwrite=True)
    assert p1 == p2


def test_fontes_diferentes_nao_colidem(tmp_path: Any) -> None:
    """AC3: docs homônimos de 2 servers no mesmo diretório viram 2 arquivos."""
    p1 = export_path(str(tmp_path), "pessoa_teste_1@example.invalid", "foto.jpg", now=UTC_MOMENT)
    p2 = export_path(str(tmp_path), "pessoa_teste_3@example.invalid", "foto.jpg", now=UTC_MOMENT)
    assert p1 != p2
    assert len(list(tmp_path.iterdir())) == 2


def test_extensao_por_content_type_unidade() -> None:
    """AC5 (unidade): Content-Type real decide; genérico/desconhecido não."""
    assert extension_for_content_type("application/pdf") == ".pdf"
    assert extension_for_content_type("text/plain; q=0.8") == ".txt"
    assert extension_for_content_type("image/jpeg") == ".jpg"
    assert extension_for_content_type("application/octet-stream") == ""
    assert extension_for_content_type("application/x-iped-desconhecido") == ""
    assert extension_for_content_type(None) == ""


def test_content_type_manda_na_extensao_com_fallback(tmp_path: Any) -> None:
    """AC5: type mapeado substitui a extensão do nome; sem mapa, mantém-na."""
    p_pdf = export_path(
        str(tmp_path), "s", "arquivo.dat", content_type="application/pdf", now=UTC_MOMENT
    )
    assert p_pdf.endswith("arquivo.pdf")
    p_dat = export_path(
        str(tmp_path), "s", "arquivo.dat", content_type="application/octet-stream",
        now=UTC_MOMENT,
    )
    assert p_dat.endswith("arquivo.dat")


def test_utc_now_e_iso_utc() -> None:
    """Hora dos exports é UTC aware e os campos internos fecham com `Z`."""
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert ISO_UTC_RE.match(iso_utc(now))


# ------------------------------------------------- default IPED_MCP_EXPORTS_DIR


def test_exports_dir_vem_do_env() -> None:
    """`IPED_MCP_EXPORTS_DIR` alimenta a fonte única em config.py."""
    try:
        os.environ["IPED_MCP_EXPORTS_DIR"] = "/opt/exports-m10"
        importlib.reload(config)
        assert config.EXPORTS_DIR == "/opt/exports-m10"
    finally:
        os.environ.pop("IPED_MCP_EXPORTS_DIR", None)
        importlib.reload(config)
    assert config.EXPORTS_DIR == config.DEFAULT_EXPORTS_DIR


def test_core_usa_default_do_env(webapi: Any, fake_router: Any, tmp_path: Any,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 3: core (que exigia dest_dir) passa a usar `$IPED_MCP_EXPORTS_DIR`."""
    exports = tmp_path / "exports-core"
    monkeypatch.setattr(config, "EXPORTS_DIR", str(exports))
    result = doc_export(SOURCE_ID, DOC_TEXT_ID)
    assert "no_file" not in result
    assert str(exports) in result["path"]
    assert os.path.getsize(result["path"]) > 0
    assert FILE_TS_RE.match(result["filename"])


def test_bookmarks_usam_default_do_env(webapi: Any, fake_router: Any, tmp_path: Any,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 3: download_bookmark/export_bookmark_info sem dest_dir usam o env."""
    exports = tmp_path / "exports-bm"
    monkeypatch.setattr(config, "EXPORTS_DIR", str(exports))
    webapi.bookmarks["dl-env"] = [{"source": SOURCE_ID, "ids": [DOC_TEXT_ID]}]
    dl = download_bookmark("dl-env", None, SOURCE_ID)
    assert dl["saved"] == 1 and dl["dest_dir"] == str(exports.resolve())
    info = export_bookmark_info(EXISTING_BOOKMARK, None, SOURCE_ID)
    assert str(exports) in info["json_path"]
    assert FILE_TS_RE.match(os.path.basename(info["json_path"]))


# --------------------------------------------------------- tools com FakeWebAPI


def test_doc_export_extensao_do_content_type(
    webapi: Any, fake_router: Any, tmp_path: Any
) -> None:
    """AC5 (P1-3): jar servindo PDF no /content → artefato .pdf apesar do nome."""
    webapi.content_types[DOC_TEXT_ID] = "application/pdf"
    result = doc_export(SOURCE_ID, DOC_TEXT_ID, str(tmp_path))
    assert result["path"].endswith("_foto.pdf")


def test_regex_csv_nome_utc_e_generated_utc(
    webapi: Any, fake_router: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2/AC5 (E2E): CSV com ts UTC no nome e `# generated_at` UTC literal."""
    _add_regex_docs(webapi)
    monkeypatch.setattr(regex_tool, "utc_now", lambda: UTC_MOMENT)
    out = regex_export_csv(SOURCE_ID, "TEST", output_dir=str(tmp_path))
    expected = f"20261011T123456Z_{safe_name(SOURCE_ID)}_regex_TEST.csv"
    assert os.path.basename(out["path"]) == expected
    with open(out["path"], encoding="utf-8") as fh:
        header = fh.read()
    assert "# generated_at: 2026-10-11T12:34:56Z" in header


def test_export_info_ordem_da_entrada_e_gerado_utc(
    webapi: Any, fake_router: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4/AC5: docs na sequência de entrada (não por conclusão de thread)."""
    ids = [401, 402, 403]
    for doc_id in ids:
        webapi.docs[(SOURCE_ID, doc_id)] = make_doc(doc_id, f"doc{doc_id}.txt", {})
    webapi.bookmarks["ordem"] = [{"source": SOURCE_ID, "ids": ids}]

    def slow_first(pair: tuple[str, int]) -> dict[str, Any]:
        """Doc mais antigo demora mais: `as_completed` inverteria a ordem."""
        source, doc_id = pair
        time.sleep(0.15 if doc_id == ids[0] else 0.0)
        return {
            "id": doc_id,
            "source": source,
            "properties": {},
            "text": {"content": "t", "chars": 1, "capped": False},
        }

    monkeypatch.setattr(export_info_mod, "_fetch_doc", slow_first)
    monkeypatch.setattr(export_info_mod, "utc_now", lambda: UTC_MOMENT)
    result = export_bookmark_info("ordem", str(tmp_path), SOURCE_ID)
    assert result["ok"] == 3
    with open(result["json_path"], encoding="utf-8") as fh:
        payload = json.load(fh)
    assert [d["id"] for d in payload["docs"]] == ids
    assert payload["generated_at"] == "2026-10-11T12:34:56Z"
    assert os.path.basename(result["json_path"]) == (
        f"20261011T123456Z_{safe_name(SOURCE_ID)}_ordem.json"
    )


def test_export_info_reexecucao_nao_sobrescreve(
    webapi: Any, fake_router: Any, tmp_path: Any
) -> None:
    """AC1 (E2E): dois exports do mesmo bookmark = 2 artefatos íntegros."""
    first = export_bookmark_info(EXISTING_BOOKMARK, str(tmp_path), SOURCE_ID)
    with open(first["json_path"], encoding="utf-8") as fh:
        before = fh.read()
    second = export_bookmark_info(EXISTING_BOOKMARK, str(tmp_path), SOURCE_ID)
    assert second["json_path"] != first["json_path"]
    with open(first["json_path"], encoding="utf-8") as fh:
        assert fh.read() == before


def test_export_info_text_usa_contrato_real_do_client(
    webapi: Any, fake_router: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regressao FASE 8a (M-8): export_bookmark_info NAO pode degradar o texto
    de todo doc por incompatibilidade de unpack com o TextResult de 5 campos.

    O contrato do client e NamedTuple (text, total_chars, capped, status,
    total_bytes); o artefato de cada doc deve sair com content preenchido e
    sem chave ``error`` no bloco ``text``.
    """
    from tests.conftest import TEXT_BODY

    monkeypatch.setattr(config, "EXPORTS_DIR", str(tmp_path))
    webapi.bookmarks["texto-vivo"] = [{"source": SOURCE_ID, "ids": [DOC_TEXT_ID]}]
    info = export_bookmark_info("texto-vivo", str(tmp_path), SOURCE_ID)
    with open(info["json_path"], encoding="utf-8") as f:
        payload = json.load(f)
    entry = payload["docs"][0]
    assert "error" not in entry["text"], entry["text"]
    assert entry["text"]["content"] == TEXT_BODY.decode("utf-8")
    assert entry["text"]["capped"] is False
