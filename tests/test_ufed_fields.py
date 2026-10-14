"""M-7 / DESIGN 07: INDEXED_FIELDS vira advertência + descoberta leve.

Pins: AC1 (campo fora da lista é TENTADO, com warning — nunca recusado);
AC2 (NON_INDEXED_FIELDS extinto do código e das docstrings); AC3
(ufed_list_fields mescla base ∪ campos observados na amostra); AC4 (0 hits
fora-da-lista avisa, 0 hits conhecido silencia); AC5 (campo da lista base
intocado — pin do Design 01). Fake WebAPI da suíte, nenhum acesso externo.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from iped_mcp.ufed import queries, tool
from tests.conftest import DOC_MD5_UPPER, DOC_NO_IMAGE_ID, SOURCE_ID, make_doc

EXOTIC_FIELD = "campo_exotico"
DOC_EXOTICO_ID = 4001
# CAND-Dα (FASE 3b): `hash` e campo real do fluxo ICARO, ausente da lista
# base — antes recusado client-side (ufed/tool.py:54-59), hoje tentado.
OUT_OF_LIST_ATTR = "hash"


def _add_exotic_doc(webapi: Any) -> None:
    """Acrescenta ao fake um doc com campo `ufed:` custom (descoberta AC3)."""
    webapi.docs[(SOURCE_ID, DOC_EXOTICO_ID)] = make_doc(
        DOC_EXOTICO_ID, "anexo-exotico.bin", {f"ufed:{EXOTIC_FIELD}": ["valor"]}
    )


def _search_calls(webapi: Any) -> int:
    """Numero de pedidos GET /search recebidos pelo fake."""
    return sum(
        1 for e in webapi.requests_log if e["method"] == "GET" and e["path"] == "/search"
    )


def test_campo_fora_da_lista_e_tentado_com_avisos(fake_router: Any, webapi: Any) -> None:
    """AC1+AC(d): sem ValueError, /search executado, avisos no envelope."""
    result = tool.ufed_search(SOURCE_ID, OUT_OF_LIST_ATTR, value="*")
    assert result["total"] == 0
    assert _search_calls(webapi) == 1  # consultou o jar (recusa dura abolida)
    assert any("fora da lista conhecida" in w for w in result["warnings"])
    assert any("pode não existir neste índice" in w for w in result["warnings"])


def test_campo_conhecido_com_hits_intacto_sem_aviso(fake_router: Any) -> None:
    """AC5: campo da lista base mantem o comportamento do Design 01."""
    result = tool.ufed_search(SOURCE_ID, "MD5", value=DOC_MD5_UPPER)
    assert result["total"] == 1
    assert result["docs"][0]["id"] == DOC_NO_IMAGE_ID
    assert result["warnings"] == []


def test_campo_conhecido_zero_hits_silencioso(fake_router: Any) -> None:
    """AC4: 0 hits em campo conhecido e resposta legitima — sem warning."""
    result = tool.ufed_search(SOURCE_ID, "SHA256", value="deadbeef")
    assert result["total"] == 0
    assert result["warnings"] == []


def test_attribute_com_prefixo_ufed_nao_gera_aviso(fake_router: Any) -> None:
    """Normalizar antes de consultar: 'ufed:MD5' e campo conhecido."""
    result = tool.ufed_search(SOURCE_ID, "ufed:MD5", value=DOC_MD5_UPPER)
    assert result["warnings"] == []
    assert result["total"] == 1


def test_ufed_search_continua_validando_limit(fake_router: Any) -> None:
    """A validacao de limit (1..500) permanece dura — so a de campo amoleceu."""
    with pytest.raises(ValueError, match="limit"):
        tool.ufed_search(SOURCE_ID, "MD5", value="x", limit=0)
    with pytest.raises(ValueError, match="limit"):
        tool.ufed_search(SOURCE_ID, "MD5", value="x", limit=501)


def test_non_indexed_fields_extinto_do_codigo() -> None:
    """AC2: nem atributo, nem import, nem docstring de tool menciona a lista."""
    assert not hasattr(queries, "NON_INDEXED_FIELDS")
    assert not hasattr(tool, "NON_INDEXED_FIELDS")
    for module in (queries, tool):
        assert "NON_INDEXED_FIELDS" not in inspect.getsource(module)
    assert "não indexados" not in str(tool.ufed_list_fields.__doc__)
    assert "HTTP 500" not in str(tool.ufed_list_fields.__doc__)


def test_list_fields_sem_source_devolve_apenas_base(fake_router: Any) -> None:
    """Sem source_id nenhuma busca e feita: so a lista conhecida, amostra 0."""
    result = tool.ufed_list_fields()
    assert result["known_fields"] == queries.KNOWN_FIELDS
    assert result["fields"] == queries.KNOWN_FIELDS
    assert result["observed_fields"] == []
    assert result["sampled"] == 0
    assert "not_indexed" not in result


def test_list_fields_mescla_base_com_amostra(fake_router: Any, webapi: Any) -> None:
    """AC3: campo custom do doc amostrado aparece na mescla, fora da base."""
    _add_exotic_doc(webapi)
    result = tool.ufed_list_fields(source_id=SOURCE_ID, sample_size=5)
    assert EXOTIC_FIELD in result["observed_fields"]
    assert EXOTIC_FIELD not in result["known_fields"]
    assert result["fields"] == list(queries.KNOWN_FIELDS) + [EXOTIC_FIELD]
    assert result["sampled"] == 4  # 3 docs padrao + 1 exotico
    assert result["warnings"] == []


def test_list_fields_cache_pula_segunda_amostra(fake_router: Any, webapi: Any) -> None:
    """Amostragem cacheada (~5 min): a 2a chamada nao repete a match-all."""
    _add_exotic_doc(webapi)
    tool.ufed_list_fields(source_id=SOURCE_ID, sample_size=5)
    calls = _search_calls(webapi)
    result = tool.ufed_list_fields(source_id=SOURCE_ID, sample_size=5)
    assert _search_calls(webapi) == calls
    assert EXOTIC_FIELD in result["fields"]


def test_list_fields_source_sem_docs_avisa_sem_ids(
    fake_router: Any, webapi: Any
) -> None:
    """Amostra vazia: aviso explicito, base devolvida, envelope intacto."""
    webapi.docs.clear()
    result = tool.ufed_list_fields(source_id=SOURCE_ID)
    assert result["sampled"] == 0
    assert result["fields"] == queries.KNOWN_FIELDS
    assert any("amostra" in w for w in result["warnings"])


def test_list_fields_valida_sample_size(fake_router: Any) -> None:
    """sample_size fora de 1..200 com source_id e erro de contrato."""
    with pytest.raises(ValueError, match="sample_size"):
        tool.ufed_list_fields(source_id=SOURCE_ID, sample_size=0)
    with pytest.raises(ValueError, match="sample_size"):
        tool.ufed_list_fields(source_id=SOURCE_ID, sample_size=201)


def test_device_info_envelope_ganha_warnings_vazio(fake_router: Any) -> None:
    """EntryName e campo conhecido: device_info segue silencioso (AC4/AC5)."""
    result = tool.ufed_device_info(SOURCE_ID)
    assert result["total"] == 0
    assert result["items"] == []
    assert result["warnings"] == []
