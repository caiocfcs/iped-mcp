"""M-18: golden + derivacao da whatsapp_reference a partir dos builders.

Protege a equivalencia byte-a-byte da referencia apos a eliminacao da
replica literal dos builders canonicos (tests/golden/whatsapp_reference.json
capturada do codigo pre-M-18) e garante que os literais de query nao
voltaram a viver na tool.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from iped_mcp.whatsapp.queries import (
    PRIVATE_NAME_RE,
    build_account_query,
    build_all_wa_query,
    build_audio_term_query,
    build_contacts_query,
    build_conversation_query,
    build_conversation_window_query,
    build_date_range_query,
    build_group_query,
    build_marker_query,
    build_media_by_hash,
    build_media_mime_query,
    build_messages_query,
    build_pdf_query,
    build_private_query,
)
from iped_mcp.whatsapp.tool import (
    _reference_canonical_filters,
    _reference_name_patterns,
    _reference_type_markers,
    whatsapp_overview,
    whatsapp_reference,
)

GOLDEN = Path(__file__).parent / "golden" / "whatsapp_reference.json"


def _dump(data: dict[str, Any]) -> str:
    """Serializacao canonica usada no golden (indentada, chaves ordenadas)."""
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_reference_matches_golden_byte_for_byte() -> None:
    """A saida da referencia e identica byte a byte a captura pre-M-18."""
    assert _dump(whatsapp_reference()) == GOLDEN.read_text(encoding="utf-8")


def test_canonical_filters_are_builder_outputs() -> None:
    """Cada filtro canônico e exatamente a saida do seu builder unico."""
    date_range = (
        "Communication\\:Date:[A TO B] (superior date-only → T23:59:59Z)"
    )
    assert whatsapp_reference()["canonical_filters"] == {
        "all_whatsapp": build_all_wa_query(),
        "messages": build_messages_query(),
        "group_messages": build_group_query(),
        "private_messages": build_private_query(),
        "private_conversation": build_conversation_query("<número>"),
        "date_range": date_range,
        "account": build_account_query(),
        "contacts": build_contacts_query(),
        "media_by_hash": build_media_by_hash("<HASH>"),
        "audio_transcription": build_audio_term_query("<termo>"),
        "pdf_with_text": build_pdf_query(),
    }


def test_type_markers_are_builder_outputs() -> None:
    """Marcadores de tipo saem de build_marker_query/build_media_mime_query."""
    markers = whatsapp_reference()["type_markers"]
    for kind in ("audio", "image", "video"):
        assert markers[kind] == build_marker_query(kind)
    assert markers["pdf"] == build_media_mime_query("application/pdf")


def test_private_key_regex_is_the_canonical_pattern() -> None:
    """A regex publicada na referencia e o proprio padrao de queries.py."""
    patterns = whatsapp_reference()["name_patterns"]
    assert patterns["private_key_regex"] == PRIVATE_NAME_RE.pattern


def test_builder_exact_strings_replace_old_replicas() -> None:
    """Pins byte-a-byte: os builders produzem as strings exatas da replica."""
    assert build_all_wa_query() == "accountType:WhatsApp"
    assert build_messages_query() == (
        'accountType:WhatsApp AND category:"Instant Messages"'
    )
    assert build_group_query() == "isGroupMessage:true"
    assert build_private_query() == (
        'accountType:WhatsApp AND category:"Instant Messages" '
        "AND -isGroupMessage:true"
    )
    assert build_conversation_query("<número>") == (
        'accountType:WhatsApp AND category:"Instant Messages" '
        "AND -isGroupMessage:true AND name:Chat AND name:<número>"
    )
    assert build_contacts_query() == "accountType:WhatsApp AND name:Contact"
    assert build_account_query() == (
        'contentType:"application/x-whatsapp-account"'
    )
    assert build_date_range_query("A", "B") == "Communication\\:Date:[A TO B]"
    assert build_media_by_hash("<HASH>") == "ufed\\:SHA256:<HASH>"
    assert build_audio_term_query("<termo>") == "audio\\:transcription:<termo>"
    assert build_marker_query("audio") == 'Message-Body:"! AUDIO_MESSAGE"'
    assert build_media_mime_query("application/pdf") == (
        "mediaMime:application/pdf"
    )
    assert build_pdf_query() == 'category:"PDF Documents" AND size:[1 TO *]'


def test_conversation_window_query_unchanged_bytes() -> None:
    """Refactor do window builder nao muda um byte da query final."""
    assert build_conversation_window_query("5511", "A", "B") == (
        'accountType:WhatsApp AND category:"Instant Messages" '
        "AND -isGroupMessage:true AND name:Chat AND name:5511 "
        "AND Communication\\:Date:[A TO B]"
    )


def test_no_canonical_query_literals_in_reference_source() -> None:
    """Os literais de query sumiram da referencia: so chamadas a builders.

    _reference_pitfalls e excluida de proposito: as armadilhas citam as
    queries em prosa documental, nao como codigo executado.
    """
    src = "".join(
        inspect.getsource(fn)
        for fn in (
            whatsapp_reference,
            _reference_canonical_filters,
            _reference_name_patterns,
            _reference_type_markers,
        )
    )
    for banned in (
        "accountType:",
        "isGroupMessage:",
        "Instant Messages",
        "Message-Body:",
        "ufed\\:",
        "audio\\:transcription",
        "mediaMime:",
        "PDF Documents",
        "x-whatsapp-account",
        "Communication\\:Date",
        "name:Contact",
        "name:Chat",
    ):
        assert banned not in src, f"literal voltou para a referencia: {banned}"


def test_no_literal_queries_in_overview_source() -> None:
    """As duas replicas do overview viraram calls de builder (M-18)."""
    src = inspect.getsource(whatsapp_overview)
    assert "accountType:" not in src
    assert "Instant Messages" not in src
