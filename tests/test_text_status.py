"""DESIGN 08 / M-8 — /text: status como dado, cap configurável, capped propagado.

Cobre os critérios de aceite:
- AC1: /text 500 → estado no_text sem exceção (a fixture FakeWebAPI devolve
  o 500 Grizzly do jar para doc inexistente — 2d 10); 404 continua exceção
  (pino em test_webapi_contract).
- AC2: cap por env `IPED_MCP_TEXT_CAP` altera o limite efetivo; default 10 MB.
- AC3: capped presente em todo retorno (tool core) e contado na busca whatsapp.
- AC4: artefato do export whatsapp com texto > cap contém o marcador literal
  "[TRUNCADO em N bytes — capped]".
- AC5: contrato de sucesso retrocompatível (campo `text` preservado).
"""

from __future__ import annotations

import importlib
import json
import os
from typing import Any, Iterator

import pytest

from iped_mcp import config
from iped_mcp import fetch
from iped_mcp.client import TextResult
from iped_mcp.core.tool import doc_text
from iped_mcp.whatsapp import tool as wa_tool
from iped_mcp.whatsapp.collect import MessageRecord
from iped_mcp.whatsapp.queries import build_pdf_query
from tests.conftest import DOC_TEXT_ID, MISSING_DOC_ID, SOURCE_ID, TEXT_BODY

TEXT_BODY_BYTES = len(TEXT_BODY)  # corpo fake de 9.500 B: cap pequeno o trunca
NUMBER = "5511999999999"


@pytest.fixture()
def restore_config() -> Iterator[None]:
    """Restaura o ambiente e recarrega o config no teardown (padrão M-4).

    O snapshot é restaurado explicitamente no teardown — a ordem de
    finalização entre monkeypatch e este fixture não é garantida.
    """
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
        importlib.reload(config)


def _result_ok() -> TextResult:
    """TextResult de sucesso com 3 caracteres lidos."""
    return TextResult("abc", 3, False, "ok", 3)


def _capped_entry(text: str = "TEXTO-PDF", capped: bool = True) -> dict[str, Any]:
    """Entrada de mídia no formato de `_fetch_media` (anexo PDF truncado)."""
    return {
        "transcription": None,
        "attachment_text": text,
        "attachment_status": "ok",
        "attachment_capped": capped,
        "attachment_bytes": 10485760,
    }


class TestCapConfiguravel:
    """AC2: cap de leitura vira constante com env, default 10 MB preservado."""

    def test_default_do_cap_e_dez_mb(self) -> None:
        """Default PRESERVADO: o literal histórico do client (10 MB)."""
        assert config.TEXT_CAP_BYTES == 10 * 1024 * 1024

    def test_env_muda_limite_efetivo_da_leitura(
        self, restore_config: None, monkeypatch: pytest.MonkeyPatch, webapi: Any
    ) -> None:
        """IPED_MCP_TEXT_CAP pequeno faz a mesma resposta voltar capped=True."""
        monkeypatch.setenv("IPED_MCP_TEXT_CAP", "64")
        importlib.reload(config)
        from iped_mcp.client import IPEDClient

        result = IPEDClient(webapi.base_url).get_document_text(SOURCE_ID, DOC_TEXT_ID)
        assert result.status == "ok"
        assert result.capped is True
        assert 64 <= result.total_bytes <= 64 + 65536  # para no chunk seguinte

    @pytest.mark.parametrize("raw", ["0", "-5", "abc"])
    def test_cap_env_invalido_recusado(
        self,
        restore_config: None,
        monkeypatch: pytest.MonkeyPatch,
        raw: str,
    ) -> None:
        """Cap inválido falha na leitura do config, nunca em produção muda."""
        monkeypatch.setenv("IPED_MCP_TEXT_CAP", raw)
        with pytest.raises(ValueError):
            importlib.reload(config)


class TestDocTextTool:
    """AC1/AC3/AC5: tool expõe status/capped sem exceção para no_text."""

    def test_status_ok_preserva_contrato_legado(self, fake_router: Any) -> None:
        out = doc_text(SOURCE_ID, DOC_TEXT_ID)
        assert out["status"] == "ok"
        assert out["text"] == TEXT_BODY.decode("utf-8")  # AC5: campo `text`
        assert out["capped"] is False
        assert out["total_chars"] == len(out["text"])

    def test_500_de_sem_texto_vira_estado_sem_excecao(self, fake_router: Any) -> None:
        """AC1: o 500 Grizzly do jar (doc sem texto) chega como status no_text."""
        out = doc_text(SOURCE_ID, MISSING_DOC_ID)
        assert out["status"] == "no_text"
        assert out["no_text"] is True  # marcador legado preservado
        assert out["capped"] is False
        assert "iped_doc_export" in out["note"]

    def test_200_vazio_e_estado_distinto_de_sem_texto(self, fake_router: Any) -> None:
        """Risco do design: no_text (500) e texto vazio ("") não se confundem."""
        fetch.text_cache_set(
            SOURCE_ID, 4242, TextResult("", 0, False, "ok", 0)
        )
        out = doc_text(SOURCE_ID, 4242)
        assert out["status"] == "no_text"
        assert "200 sem corpo" in out["note"]

    def test_offset_alem_do_fim_mantem_no_text(self, fake_router: Any) -> None:
        """Comportamento legado pinado: offset fora do texto é estado, não erro."""
        out = doc_text(SOURCE_ID, DOC_TEXT_ID, offset=10**9)
        assert out["status"] == "no_text"
        assert "offset" in out["note"]

    def test_cap_do_env_aparece_na_tool(
        self,
        fake_router: Any,
        restore_config: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC3: capped=true no retorno da tool + nota com o teto efetivo."""
        monkeypatch.setenv("IPED_MCP_TEXT_CAP", "64")
        importlib.reload(config)
        out = doc_text(SOURCE_ID, DOC_TEXT_ID)
        assert out["status"] == "ok"
        assert out["capped"] is True
        assert "IPED_MCP_TEXT_CAP" in out["note"]


def _patch_media_source(
    monkeypatch: pytest.MonkeyPatch, result: TextResult | None
) -> None:
    """_fetch_media sobre um único doc PDF cujo /text devolve `result`."""
    monkeypatch.setattr(wa_tool, "search_ids", lambda q, s, h: [53])
    monkeypatch.setattr(
        wa_tool, "get_document_props", lambda s, d: {"category": ["PDF Documents"]}
    )
    monkeypatch.setattr(wa_tool, "get_document_text_cached", lambda s, d: result)


class TestColetaClassifica:
    """Coleta whatsapp CLASSIFICA no_text/unavailable e segue (padrão icaro)."""

    def test_anexo_ok_capped_classificado(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_media_source(monkeypatch, TextResult("abc", 3, True, "ok", 9_999_999))
        media = wa_tool._fetch_media(SOURCE_ID, ["h1"])
        entry = media["h1"]
        assert entry["attachment_status"] == "ok"
        assert entry["attachment_text"] == "abc"
        assert entry["attachment_capped"] is True
        assert entry["attachment_bytes"] == 9_999_999

    def test_no_text_e_dado_e_a_coleta_segue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_media_source(monkeypatch, TextResult("", 0, False, "no_text", 0))
        media = wa_tool._fetch_media(SOURCE_ID, ["h1"])
        assert media["h1"]["attachment_status"] == "no_text"
        assert media["h1"]["attachment_text"] is None

    def test_falha_de_leitura_e_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_media_source(monkeypatch, None)
        media = wa_tool._fetch_media(SOURCE_ID, ["h1"])
        assert media["h1"]["attachment_status"] == "unavailable"


class TestExportMarcador:
    """AC4: o export registra o truncamento no artefato e nos campos."""

    def test_attachment_for_grava_marcador_no_texto(self) -> None:
        rec = MessageRecord(1, "contato", NUMBER, "chat", None, None, None, ["h1"])
        text, status, capped = wa_tool._attachment_for(rec, {"h1": _capped_entry()})
        assert status == "ok" and capped is True
        assert text is not None
        assert text.startswith("TEXTO-PDF\n\n")
        assert text.endswith("[TRUNCADO em 10485760 bytes — capped]")

    def test_attachment_sem_cap_sem_marcador(self) -> None:
        rec = MessageRecord(1, "contato", NUMBER, "chat", None, None, None, ["h1"])
        text, _status, capped = wa_tool._attachment_for(
            rec, {"h1": _capped_entry(capped=False)}
        )
        assert capped is False
        assert text == "TEXTO-PDF"

    def test_json_do_export_contem_marcador(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E2E da tool: JSON gravado traz texto + marcador + campos de estado."""
        num = wa_tool._require_number(NUMBER)
        rec = MessageRecord(1, "contato", num, "chat", None, None, None, ["h1"])
        monkeypatch.setattr(wa_tool, "collect_messages", lambda s: ([rec], {}, 0))
        monkeypatch.setattr(
            wa_tool,
            "fetch_docs_parallel",
            lambda s, ids, **kw: [
                (1, {"Message-Body": ["oi"], "Communication:Date": ["2024-01-01T00:00:00Z"]})
            ],
        )
        monkeypatch.setattr(
            wa_tool, "_fetch_media", lambda s, hs: {"h1": _capped_entry()}
        )
        out = wa_tool.whatsapp_export_conversation(
            SOURCE_ID, NUMBER, output_dir=str(tmp_path)
        )
        with open(out["path"], encoding="utf-8") as fh:
            payload = json.load(fh)
        msg = payload["messages"][0]
        assert msg["attachment_status"] == "ok"
        assert msg["attachment_capped"] is True
        assert "TRUNCADO em 10485760 bytes" in msg["attachment_text"]


class TestTextSearchSegueComNoText:
    """Busca por substring em PDF: no_text é classificado, o fluxo segue."""

    def _patch_layers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            wa_tool, "collect_messages", lambda s: ([], {"h1": {NUMBER}}, 0)
        )

        def fake_search(q: str, s: str, h: str) -> list[int]:
            return [53] if q == build_pdf_query() else []

        monkeypatch.setattr(wa_tool, "search_ids", fake_search)
        monkeypatch.setattr(
            wa_tool,
            "fetch_docs_parallel",
            lambda s, ids, **kw: [(53, {"ufed:SHA256": ["h1"]})] if ids else [],
        )

    def test_pdf_sem_texto_nao_aborta_busca(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_layers(monkeypatch)
        monkeypatch.setattr(
            wa_tool,
            "get_document_text_cached",
            lambda s, d: TextResult("", 0, False, "no_text", 0),
        )
        out = wa_tool.whatsapp_text_search(SOURCE_ID, "pix")
        assert out["conversations"]["attachment"] == 0

    def test_pdf_com_texto_continua_contando(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_layers(monkeypatch)
        monkeypatch.setattr(
            wa_tool,
            "get_document_text_cached",
            lambda s, d: TextResult("paguei o PIX ontem", 19, False, "ok", 19),
        )
        out = wa_tool.whatsapp_text_search(SOURCE_ID, "pix")
        assert out["conversations"]["attachment"] == 1
