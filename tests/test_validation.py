"""M-14: validacao uniforme de entrada entre tools + idsfile sem ambiguidade.

Pins: (1) helpers unicos de `validation` (faixa, minimo, contagem) com erro
claro que nomeia o parametro; (2) tetos por tool — limites novos onde nao
havia teto (regex_value_docs, top, *_from_file) e tetos historicos movidos
para o modulo unico sem mudar o valor; (3) idsfile com doc-ID primeiro e
prefixo explicito "md5:". A revogacao do ServerError tipado do router tem
pino proprio em test_error_policy.py; o nao-retry de 5xx em busca continua
pinado em test_resilience.py (M-3/AC4). Fake WebAPI da suite, nenhuma rede
externa.
"""

from __future__ import annotations

from typing import Any

import pytest

from iped_mcp import validation
from iped_mcp.bookmarks import idsfile
from iped_mcp.bookmarks import tool as bookmarks_tool
from iped_mcp.core import tool as core_tool
from iped_mcp.regex import tool as regex_tool
from iped_mcp.ufed import tool as ufed_tool
from iped_mcp.whatsapp import tool as wa_tool
from tests.conftest import SOURCE_ID


# --- helpers do modulo validation -----------------------------------------


class TestCheckIntRange:
    def test_faixa_aceita_bordas(self) -> None:
        assert validation.check_int_range(1, "limit", 1, 500) == 1
        assert validation.check_int_range(500, "limit", 1, 500) == 500

    def test_fora_da_faixa_nomeia_parametro_e_valor(self) -> None:
        with pytest.raises(ValueError, match="limit deve estar entre 1 e 500"):
            validation.check_int_range(501, "limit", 1, 500)
        with pytest.raises(ValueError, match=r"recebido: 0"):
            validation.check_int_range(0, "limit", 1, 500)

    def test_bool_e_recusada_como_invalida(self) -> None:
        # bool e subclasse de int; nenhuma contagem das tools aceita bool.
        with pytest.raises(ValueError, match="inteiro"):
            validation.check_int_range(True, "limit", 1, 500)

    def test_nao_inteiro_e_recusado(self) -> None:
        with pytest.raises(ValueError, match="max_chars"):
            validation.check_int_range("50", "max_chars", 1, 100)


class TestCheckMin:
    def test_minimo_inclusivo(self) -> None:
        assert validation.check_min(0, "offset", 0) == 0

    def test_abaixo_do_minimo_nomeia_parametro(self) -> None:
        with pytest.raises(ValueError, match="offset deve ser >= 0"):
            validation.check_min(-1, "offset", 0)


class TestCheckCount:
    def test_no_teto_passa(self) -> None:
        validation.check_count(500, "docs", 500)

    def test_acima_nomeia_contagem_teto_e_parametro(self) -> None:
        with pytest.raises(ValueError, match=r"501 docs excede o teto de 500"):
            validation.check_count(501, "docs", 500)

    def test_hint_vai_para_a_mensagem(self) -> None:
        with pytest.raises(ValueError, match="bookmarks_add_from_file"):
            validation.check_count(
                600, "docs", 500, hint="Use bookmarks_add_from_file."
            )


# --- tetos por tool (valores historicos preservados; tetos novos pinados) --


class TestTetosSearch:
    def test_core_search_teto_500(self, fake_router: Any) -> None:
        assert core_tool.search("*", SOURCE_ID, limit=500)["total"] == 3
        with pytest.raises(ValueError, match="limit deve estar entre 1 e 500"):
            core_tool.search("*", SOURCE_ID, limit=501)

    def test_core_doc_text_limites(self, fake_router: Any) -> None:
        from tests.conftest import ALL_DOC_IDS

        doc_id = ALL_DOC_IDS[0]
        assert core_tool.doc_text(SOURCE_ID, doc_id, max_chars=100000)["status"]
        with pytest.raises(ValueError, match="max_chars deve estar entre"):
            core_tool.doc_text(SOURCE_ID, doc_id, max_chars=100001)
        with pytest.raises(ValueError, match="offset deve ser >= 0"):
            core_tool.doc_text(SOURCE_ID, doc_id, offset=-1)

    def test_ufed_search_teto_500(self, fake_router: Any) -> None:
        with pytest.raises(ValueError, match="limit deve estar entre 1 e 500"):
            ufed_tool.ufed_search(SOURCE_ID, "MD5", value="x", limit=501)

    def test_ufed_list_fields_teto_amostra_200(self) -> None:
        with pytest.raises(ValueError, match="sample_size deve estar entre 1 e 200"):
            ufed_tool.ufed_list_fields(source_id=SOURCE_ID, sample_size=201)


class TestTetosRegex:
    def test_value_docs_ganhou_teto_500(self, fake_router: Any) -> None:
        # Antes do M-14: NENHUM teto (D-08). Agora rejeita ANTES da busca.
        with pytest.raises(ValueError, match="limit deve estar entre 1 e 500"):
            regex_tool.regex_value_docs(SOURCE_ID, "TEST", "v", limit=501)
        assert regex_tool.regex_value_docs(
            SOURCE_ID, "TEST", "v", limit=500
        )["docs"] is not None

    def test_top_values_ganhou_teto_1000(self, fake_router: Any) -> None:
        # Antes do M-14: so `top >= 1`, sem teto superior.
        with pytest.raises(ValueError, match="top deve estar entre 1 e 1000"):
            regex_tool.regex_top_values(SOURCE_ID, "TEST", top=1001)
        assert "top" in regex_tool.regex_top_values(SOURCE_ID, "TEST", top=1000)

    def test_list_types_amostra_continua_ate_2000(self, fake_router: Any) -> None:
        with pytest.raises(ValueError, match="sample_size deve estar entre 1 e 2000"):
            regex_tool.regex_list_types(SOURCE_ID, sample_size=2001)
        assert regex_tool.regex_list_types(SOURCE_ID, sample_size=2000)


class TestTetosWhatsapp:
    def test_export_limit_teto_500(self) -> None:
        with pytest.raises(ValueError, match="limit deve estar entre 1 e 500"):
            wa_tool.whatsapp_export_conversation(SOURCE_ID, "5511", limit=501)

    def test_overview_top_ganhou_teto_1000(self) -> None:
        with pytest.raises(ValueError, match="top deve estar entre 1 e 1000"):
            wa_tool.whatsapp_overview(SOURCE_ID, top=1001)


class TestTetosBookmarks:
    def _docs(self, n: int) -> list[dict[str, Any]]:
        return [{"source": SOURCE_ID, "id": i} for i in range(1, n + 1)]

    def test_docs_pagina_teto_500(self, fake_router: Any) -> None:
        with pytest.raises(ValueError, match="limit deve estar entre 1 e 500"):
            bookmarks_tool.bookmarks_docs("bm", SOURCE_ID, limit=501)

    def test_add_json_teto_500_com_hint_do_from_file(self) -> None:
        with pytest.raises(ValueError, match="excede o teto de 500") as exc:
            bookmarks_tool.bookmarks_add("bm", self._docs(501), SOURCE_ID)
        assert "bookmarks_add_from_file" in str(exc.value)

    def test_remove_json_teto_500_com_hint_do_from_file(self) -> None:
        with pytest.raises(ValueError, match="excede o teto de 500") as exc:
            bookmarks_tool.bookmarks_remove("bm", self._docs(501), SOURCE_ID)
        assert "bookmarks_remove_from_file" in str(exc.value)

    def test_save_search_teto_1000(self) -> None:
        with pytest.raises(ValueError, match="limit deve estar entre 1 e 1000"):
            bookmarks_tool.bookmarks_save_search("bm", "*", SOURCE_ID, limit=1001)

    def test_report_teto_50(self) -> None:
        with pytest.raises(ValueError, match="limit deve estar entre 1 e 50"):
            bookmarks_tool.bookmarks_report("bm", SOURCE_ID, limit=51)

    def _write(self, tmp_path: Any, n: int) -> str:
        path = tmp_path / "ids.txt"
        path.write_text("".join(f"{i}\n" for i in range(1, n + 1)), encoding="utf-8")
        return str(path)

    def test_add_from_file_rejeita_acima_do_teto_de_arquivo(
        self, tmp_path: Any
    ) -> None:
        # D-08: o fluxo de arquivo nao tinha teto nenhum.
        path = self._write(tmp_path, validation.FILE_IDS_MAX + 1)
        with pytest.raises(ValueError, match=r"ids/md5 no arquivo excede o teto"):
            bookmarks_tool.bookmarks_add_from_file("bm", path, SOURCE_ID)

    def test_remove_from_file_rejeita_acima_do_teto_de_arquivo(
        self, tmp_path: Any
    ) -> None:
        path = self._write(tmp_path, validation.FILE_IDS_MAX + 1)
        with pytest.raises(ValueError, match="ids/md5 no arquivo"):
            bookmarks_tool.bookmarks_remove_from_file("bm", path, SOURCE_ID)

    def test_create_from_file_rejeita_acima_do_teto_de_arquivo(
        self, tmp_path: Any
    ) -> None:
        path = self._write(tmp_path, validation.FILE_IDS_MAX + 1)
        with pytest.raises(ValueError, match="ids/md5 no arquivo"):
            bookmarks_tool.bookmarks_create_from_file("bm", path, "fonte")

    def test_add_from_file_aceita_no_teto(self, fake_router: Any, tmp_path: Any) -> None:
        path = self._write(tmp_path, validation.FILE_IDS_MAX)
        result = bookmarks_tool.bookmarks_add_from_file("bm-limite", path, SOURCE_ID)
        assert result["sent"] == validation.FILE_IDS_MAX


# --- idsfile: doc-ID primeiro + prefixo "md5:" (M-14) ----------------------


MD5_NUMERIC = "1" * 32  # MD5 formado so por digitos (~10^-7 dos hashes)
MD5_LETTERS = "2911f8019ea1915573d42899ee32a90c"


def _read(tmp_path: Any, content: str) -> tuple[list[int], list[str], int]:
    path = tmp_path / "lista.txt"
    path.write_text(content, encoding="utf-8")
    return idsfile.read_ids_or_hashes_file(str(path))


class TestIdsFile:
    def test_trinta_e_dois_digitos_agora_e_doc_id(self, tmp_path: Any) -> None:
        # Pino da INVERSAO: o baseline tratava como MD5 (D-08).
        ids, md5s, invalid = _read(tmp_path, MD5_NUMERIC + "\n")
        assert ids == [int(MD5_NUMERIC)]
        assert md5s == []
        assert invalid == 0

    def test_md5_hex_com_letras_continua_nu(self, tmp_path: Any) -> None:
        ids, md5s, invalid = _read(tmp_path, MD5_LETTERS.upper() + "\n")
        assert ids == []
        assert md5s == [MD5_LETTERS]  # normalizado p/ minusculo
        assert invalid == 0

    def test_prefixo_md5_forca_hash_somente_numericos(self, tmp_path: Any) -> None:
        ids, md5s, invalid = _read(tmp_path, f"md5:{MD5_NUMERIC}\n")
        assert ids == []
        assert md5s == [MD5_NUMERIC]

    def test_prefixo_case_insensitive_e_com_espacos(self, tmp_path: Any) -> None:
        _, md5s, invalid = _read(tmp_path, f"  MD5: {MD5_LETTERS.upper()} \n")
        assert md5s == [MD5_LETTERS]
        assert invalid == 0

    def test_prefixo_md5_com_conteudo_invalido_e_contado(self, tmp_path: Any) -> None:
        _, md5s, invalid = _read(tmp_path, "md5:nao-e-hash\n")
        assert md5s == []
        assert invalid == 1

    def test_mistura_deduplicada_preservando_ordem(self, tmp_path: Any) -> None:
        content = (
            f"42\n{MD5_LETTERS}\n42\n\n{MD5_NUMERIC}\nmd5:{MD5_LETTERS}\nlixo!\n"
        )
        ids, md5s, invalid = _read(tmp_path, content)
        assert ids == [42, int(MD5_NUMERIC)]
        assert md5s == [MD5_LETTERS]  # nu + prefixado deduplicam
        assert invalid == 1

    def test_arquivo_inexistente_nomeia_o_path(self, tmp_path: Any) -> None:
        missing = str(tmp_path / "nao-existe.txt")
        with pytest.raises(ValueError, match="nao-existe.txt"):
            idsfile.read_ids_or_hashes_file(missing)
