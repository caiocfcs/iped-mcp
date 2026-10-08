"""Queries canônicas e extração de chaves do WhatsApp no IPED.

Toda query/regex aqui foi validada contra sources reais de WhatsApp.
Os builders só emitem a sintaxe Lucene segura (escape `\\:`, aspas em valores
com `:`, negação na posição `X AND -Y AND Z` — `X -Y AND Z` descarta X).
"""

from __future__ import annotations

import re
from datetime import datetime

from iped_mcp.lucene import quote_value
from iped_mcp.ufed.queries import build_ufed_query

# --- padrões de `name` (validados em F1 D1.3 + F2 D2.2/D2.3) -----------------

# privada: `WhatsApp Chat - <contato> - <número>_message_<n>`;
# iPhone c/ segmento extra: `… - <número>_0_message_<n>` (F2.2);
# Android sem nome: `WhatsApp Chat - <número>_message_<n>` (F2.3).
PRIVATE_NAME_RE = re.compile(r"^WhatsApp Chat - (?:.+ - )?(\d+)(?:_\d+)?_message_\d+$")
GROUP_NAME_RE = re.compile(r"^WhatsApp Group - (?:.+ - )?(\d+)(?:_\d+)?_message_\d+$")
CHANNEL_NAME_RE = re.compile(r"^WhatsApp Channel - (?:.+ - )?(\d+)(?:_\d+)?_message_\d+$")
STATUS_NAME_RE = re.compile(r"^WhatsApp Status - (?:.+ - )?(\d+)(?:_\d+)?_message_\d+$")

# display name da conversa privada (ausente no Android sem nome → None)
_PRIVATE_DISPLAY_RE = re.compile(r"^WhatsApp Chat - (.+) - \d+(?:_\d+)?_message_\d+$")

# --- extração de número de `Communication:From/To` (4 formatos, F2.4) --------

_FROM_TO_PAREN_RE = re.compile(r"\((\d{8,15})(?:@s\.whatsapp\.net)?\)\s*$")
_FROM_TO_JID_RE = re.compile(r"^(\d{8,15})(?:@s\.whatsapp\.net)?$")

# --- marcadores de tipo em `Message-Body` (validados em F1 D1.4) -------------

MARKERS = {
    "audio": "! AUDIO_MESSAGE",
    "image": "! IMAGE_MESSAGE",
    "video": "! VIDEO_MESSAGE",
}


def classify_name(name: str) -> tuple[str, str | None]:
    """Classifica o `name` → (tipo, chave).

    tipo: "chat" | "group" | "channel" | "status" | "other";
    chave: número (privada) ou JID (grupo/canal/status).
    """
    for kind, regex in (
        ("chat", PRIVATE_NAME_RE),
        ("group", GROUP_NAME_RE),
        ("channel", CHANNEL_NAME_RE),
        ("status", STATUS_NAME_RE),
    ):
        m = regex.match(name)
        if m:
            return kind, m.group(1)
    return "other", None


def extract_conversation_key(name: str) -> str | None:
    """Número da conversa privada no `name` (None se não for privada)."""
    m = PRIVATE_NAME_RE.match(name)
    return m.group(1) if m else None


def display_name(name: str) -> str | None:
    """Nome de exibição da conversa privada (None no Android sem nome)."""
    m = _PRIVATE_DISPLAY_RE.match(name)
    return m.group(1) if m else None


def extract_number(value: str | None) -> str | None:
    """Extrai o número de `Communication:From/To` (4 formatos, F2.4).

    `Nome (5521…)` · `Nome (5521…@s.whatsapp.net)` · JID puro `5522…@s.whatsapp.net`
    · ausente → None.
    """
    if not value:
        return None
    value = str(value)
    m = _FROM_TO_PAREN_RE.search(value) or _FROM_TO_JID_RE.match(value)
    return m.group(1) if m else None


def normalize_number(value: str) -> str:
    """Normaliza um número para dígitos (aceita `+`, `00`, espaços, hífens)."""
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


# --- builders de query (strings exatas, testadas em unit) --------------------

def build_all_wa_query() -> str:
    """Tudo WhatsApp (mensagens + conta + contatos + status + chamadas)."""
    return "accountType:WhatsApp"


def build_messages_query() -> str:
    """Mensagens WA (categoria Instant Messages)."""
    return 'accountType:WhatsApp AND category:"Instant Messages"'


def build_private_query() -> str:
    """Mensagens privadas (bruto — inclui eventos de grupo/canal sem a flag, F3.2)."""
    return (
        'accountType:WhatsApp AND category:"Instant Messages" AND -isGroupMessage:true'
    )


def build_group_query() -> str:
    """Mensagens de grupo (canônico p/ "é grupo"; subconta eventos sem a flag, F2.5)."""
    return "isGroupMessage:true"


def build_conversation_query(number: str) -> str:
    """Conversa privada exata com o número (F3.6: `name:Chat` exclui Status)."""
    return (
        f'{build_private_query()} AND name:Chat AND name:{number}'
    )


def build_conversation_window_query(number: str, start: str, end: str) -> str:
    """Conversa privada exata restrita a uma janela de `Communication:Date`."""
    return (
        f"{build_conversation_query(number)} AND "
        f"Communication\\:Date:[{start} TO {end}]"
    )


def build_marker_query(kind: str) -> str:
    """Marcador de tipo em `Message-Body` (kind: audio | image | video)."""
    if kind not in MARKERS:
        raise ValueError(f"kind inválido '{kind}'; use: {', '.join(MARKERS)}.")
    return f'Message-Body:"{MARKERS[kind]}"'


def build_media_mime_query(mime: str) -> str:
    """`mediaMime` com valor exato (sem wildcard — F1.5)."""
    return f"mediaMime:{mime}"


def build_media_by_hash(sha256: str) -> str:
    """Doc de mídia pelo hash (case-insensitive; link unidirecional, F1.17)."""
    return f"ufed\\:SHA256:{sha256}"


def build_audio_term_query(term: str) -> str:
    """Termo na transcrição de áudio (no doc de mídia, F1.6)."""
    return f"audio\\:transcription:{quote_value(term)}"


def build_message_term_query(term: str) -> str:
    """Termo no corpo de mensagens privadas (token Lucene, F3.3)."""
    return f"{build_private_query()} AND Message-Body:{quote_value(term)}"


def build_pdf_query() -> str:
    """PDFs com texto extraível (F3.4: `size:[1 TO *]` exclui PDF vazio)."""
    return 'category:"PDF Documents" AND size:[1 TO *]'


def build_account_query() -> str:
    """Doc da conta do dono (1 por source; valor entre aspas — F1.11)."""
    return 'contentType:"application/x-whatsapp-account"'


def build_profile_query(number: str) -> str:
    """Avatar cacheado pelo número (prefixo numérico — o `@` nunca entra na query, F2.7)."""
    return build_ufed_query("Name", value=f"{number}*")


# --- normalização de datas (F2.6: date-only no fim → T23:59:59Z) -------------

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?Z?$")
_BR_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})(?:[ ](\d{2}):(\d{2})(?::(\d{2}))?)?$")


def _parse_date(raw: str) -> tuple[int, int, int, int | None, int | None, int | None]:
    m = _ISO_RE.match(raw)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _BR_RE.match(raw)
        if not m:
            raise ValueError(
                f"Data inválida '{raw}'; use ISO 8601 (AAAA-MM-DD[THH:MM[:SS]]) "
                "ou DD/MM/YYYY[ HH:MM[:SS]]."
            )
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour, minute, second = m.group(4), m.group(5), m.group(6)
    return (
        year,
        month,
        day,
        int(hour) if hour is not None else None,
        int(minute) if minute is not None else None,
        int(second) if second is not None else None,
    )


def _format_iso(
    year: int,
    month: int,
    day: int,
    hour: int | None,
    minute: int | None,
    second: int | None,
) -> str:
    if hour is None:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second or 0:02d}Z"


def normalize_range(start: str, end: str) -> tuple[str, str]:
    """Normaliza start/end para ISO 8601 UTC.

    Data-only no `end` vira `T23:59:59Z` (F2.6: sem isso o último dia é
    truncado); data-only no `start` vira `T00:00:00Z`.
    """
    s = _parse_date(start.strip())
    e = _parse_date(end.strip())
    start_iso = _format_iso(*s) or f"{s[0]:04d}-{s[1]:02d}-{s[2]:02d}T00:00:00Z"
    end_iso = _format_iso(*e) or f"{e[0]:04d}-{e[1]:02d}-{e[2]:02d}T23:59:59Z"
    if start_iso > end_iso:
        raise ValueError(f"'start' ({start_iso}) é depois de 'end' ({end_iso}).")
    return start_iso, end_iso


def bucket_key(date_iso: str, granularity: str) -> str:
    """Bucket de `Communication:Date` (month: AAAA-MM · week: AAAA-Wnn · day: AAAA-MM-DD)."""
    if granularity == "month":
        return date_iso[:7]
    if granularity == "day":
        return date_iso[:10]
    if granularity == "week":
        dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
        iso_year, iso_week, _ = dt.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    raise ValueError(f"granularity inválida '{granularity}'; use: month, week, day.")
