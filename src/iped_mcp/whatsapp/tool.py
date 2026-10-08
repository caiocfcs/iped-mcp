"""Tools de dados do WhatsApp no IPED (família `whatsapp_`).

Encapsulam as queries canônicas e armadilhas validadas em
iped-mcp(obsidian)/exemplos/testes_validados/whatsapp/ (Fases 1–3): o agente
nunca monta Lucene manualmente. Conversas de grupo ficam para a Fase 4.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import requests
from fastmcp import FastMCP

from iped_mcp.config import WORKERS
from iped_mcp.core.tool import doc_export
from iped_mcp.fetch import (
    fetch_docs_parallel,
    get_document_props,
    get_document_text,
    search_ids,
)
from iped_mcp.whatsapp.collect import MessageRecord, collect_messages
from iped_mcp.whatsapp.queries import (
    MARKERS,
    PRIVATE_NAME_RE,
    bucket_key,
    build_account_query,
    build_audio_term_query,
    build_conversation_window_query,
    build_marker_query,
    build_media_by_hash,
    build_media_mime_query,
    build_message_term_query,
    build_pdf_query,
    build_profile_query,
    classify_name,
    display_name,
    normalize_number,
    normalize_range,
)

DEFAULT_EXPORTS_DIR = "/tmp/iped_mcp/exports"
EXPORTS_DIR = os.environ.get("IPED_MCP_EXPORTS_DIR", DEFAULT_EXPORTS_DIR)

_WA_HINT = "Use whatsapp_reference para conferir os campos e queries canônicas."

_NO_WA_RESULT = {
    "has_whatsapp": False,
    "total_wa": 0,
    "total_messages": 0,
    "note": "source sem documentos WhatsApp (accountType:WhatsApp → 0).",
}


def _first(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _require_number(number: str) -> str:
    num = normalize_number(number)
    if not num:
        raise ValueError("'number' deve conter dígitos (ex.: 5511999999999).")
    if len(num) > 15:
        raise ValueError(
            f"'{number}' parece um JID de grupo (muito longo para número de "
            "celular). Tools de grupo ainda não implementadas (Fase 4)."
        )
    return num


def _owner_info(source_id: str) -> dict[str, Any] | None:
    """Doc da conta do dono (1 por source) → dict ou None."""
    ids = search_ids(build_account_query(), source_id, _WA_HINT)
    if not ids:
        return None
    props = get_document_props(source_id, ids[0]) or {}
    phone = str(_first(props.get("phoneNumber")) or "")
    return {
        "doc_id": ids[0],
        "jid": _first(props.get("userAccount")),
        "phone_number": phone or None,
        "display_name": _first(props.get("userName")),
        "status": _first(props.get("userNotes")),
        "owner_number": normalize_number(phone),
    }


def _display_name_for(records: list[MessageRecord], key: str) -> str | None:
    for rec in records:
        if rec.kind == "chat" and rec.key == key:
            name = display_name(rec.name)
            if name:
                return name
    return None


def _media_hash(props: dict[str, Any]) -> str | None:
    for field_name in ("ufed:SHA256", "sha-256"):
        value = _first(props.get(field_name))
        if value:
            return str(value).lower()
    return None


def _fetch_media(source_id: str, hashes: list[str]) -> dict[str, dict[str, Any]]:
    """Para cada sha-256: doc de mídia → transcrição (áudio) e/ou texto (PDF).

    Imagem → ambos None (F3.5: /text de imagem é vazio; sem OCR no escopo).
    """

    def _one(h: str) -> tuple[str, dict[str, Any] | None]:
        entry: dict[str, Any] = {"transcription": None, "attachment_text": None}
        try:
            ids = search_ids(build_media_by_hash(h), source_id, _WA_HINT)
            if not ids:
                return h, None
            props = get_document_props(source_id, ids[0]) or {}
            transcription = _first(props.get("audio:transcription"))
            if transcription:
                entry["transcription"] = str(transcription)
            category = _first(props.get("category"))
            content_type = _first(props.get("contentType"))
            if category == "PDF Documents" or content_type == "application/pdf":
                entry["attachment_text"] = get_document_text(source_id, ids[0])
        except (requests.RequestException, ValueError):
            return h, None
        return h, entry

    media: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for h, entry in pool.map(_one, hashes):
            if entry is not None:
                media[h] = entry
    return media


def _prepare_export_dir(output_dir: str | None) -> tuple[str, bool]:
    target = str(output_dir) if output_dir else EXPORTS_DIR
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise ValueError(
            f"Não foi possível criar o diretório de exportação '{target}': {e}"
        ) from e
    return target, output_dir is not None and os.path.isabs(str(output_dir))


# --- tools --------------------------------------------------------------------


def whatsapp_overview(source_id: str, top: int = 10) -> dict[str, Any]:
    """Triagem do WhatsApp no source: totais, período, série mensal, conversas distintas e top interlocutores.

    Responde "o que tem de WhatsApp aqui": total de docs WA, total de
    mensagens, período (início/fim), mensagens por mês, nº de conversas
    privadas/grupos/canais e as conversas mais volumosas. `top_interlocutors`
    ranqueia conversas privadas por volume (enviadas/recebidas), excluindo o
    dono e o artefato `0`. A primeira chamada é pesada (coleta completa das
    mensagens do source, minutos); as seguintes usam cache de 5 min.

    Args:
        source_id: source no IPED (veja iped_list_sources).
        top: nº de conversas/interlocutores a devolver (padrão 10).
    """
    if top < 1:
        raise ValueError("top deve ser >= 1.")
    total_wa = len(search_ids("accountType:WhatsApp", source_id, _WA_HINT))
    if total_wa == 0:
        return {"source": source_id, **_NO_WA_RESULT}
    total_messages = len(
        search_ids(
            'accountType:WhatsApp AND category:"Instant Messages"',
            source_id,
            _WA_HINT,
        )
    )
    records, _sha_map, dropped = collect_messages(source_id)
    dates = sorted(r.date for r in records if r.date)
    period = {"start": dates[0], "end": dates[-1]} if dates else None
    monthly_counter = Counter(r.date[:7] for r in records if r.date)
    monthly = [
        {"month": month, "messages": count}
        for month, count in sorted(monthly_counter.items())
    ]
    conv: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.kind in ("chat", "group", "channel") and rec.key:
            entry = conv.setdefault(
                rec.key,
                {"key": rec.key, "type": rec.kind, "messages": 0, "name": None},
            )
            entry["messages"] += 1
            if rec.kind == "chat" and not entry["name"]:
                entry["name"] = display_name(rec.name)
    conversations = {
        "private": sum(1 for e in conv.values() if e["type"] == "chat"),
        "group": sum(1 for e in conv.values() if e["type"] == "group"),
        "channel": sum(1 for e in conv.values() if e["type"] == "channel"),
    }
    conversations["total"] = conversations["private"] + conversations["group"] + conversations["channel"]
    top_conversations = sorted(conv.values(), key=lambda e: -e["messages"])[:top]
    owner = _owner_info(source_id)
    owner_number = owner["owner_number"] if owner else None
    interloc: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.kind != "chat" or not rec.key or rec.key == "0":
            continue
        if owner_number and rec.key == owner_number:
            continue
        entry = interloc.setdefault(
            rec.key,
            {"number": rec.key, "name": None, "sent": 0, "received": 0},
        )
        if rec.to_number == rec.key:
            entry["sent"] += 1
        if rec.from_number == rec.key:
            entry["received"] += 1
        if not entry["name"]:
            entry["name"] = display_name(rec.name)
    top_interlocutors = [
        {**e, "messages": e["sent"] + e["received"]}
        for e in sorted(
            interloc.values(), key=lambda e: -(e["sent"] + e["received"])
        )[:top]
    ]
    return {
        "source": source_id,
        "has_whatsapp": True,
        "total_wa": total_wa,
        "total_messages": total_messages,
        "dropped_docs": dropped,
        "period": period,
        "monthly": monthly,
        "conversations": conversations,
        "top_conversations": top_conversations,
        "top_interlocutors": top_interlocutors,
        "owner": (
            {"number": owner_number, "excluded_from_interlocutors": True}
            if owner_number
            else None
        ),
        "note": (
            "top_conversations conta por chave de name; top_interlocutors por "
            "From/To (drift = eventos de sistema sem From/To)."
        ),
    }


def whatsapp_account(source_id: str) -> dict[str, Any]:
    """Conta do dono do WhatsApp no source: JID, número, nome de exibição e status.

    O número do dono (`owner_number`) é a entrada de whatsapp_profile_photo
    (foto de perfil) e a chave de exclusão do dono em whatsapp_overview.

    Args:
        source_id: source no IPED (veja iped_list_sources).
    """
    owner = _owner_info(source_id)
    if owner is None:
        return {
            "source": source_id,
            "found": False,
            "hint": (
                "source sem doc de conta WhatsApp "
                '(contentType:"application/x-whatsapp-account"); o WhatsApp '
                "pode não estar presente."
            ),
        }
    return {"source": source_id, "found": True, **owner}


def select_profile_photo(
    candidates: list[dict[str, Any]], number: str
) -> dict[str, Any] | None:
    """Escolhe o avatar entre os candidatos (regras validadas em D2.7/D2.8).

    Descarta `.thumb`; prefere `.jpg` com timestamp (`<número>-<ts>.jpg`) de
    maior timestamp; senão `.jpg` (variante status@broadcast), `.png`
    (spotlight) ou `.j` (Android).
    """
    cands = [c for c in candidates if not str(c.get("name", "")).lower().endswith(".thumb")]
    if not cands:
        return None
    stamped_re = re.compile(rf"^{number}-(\d+)\.jpg$", re.IGNORECASE)
    stamped = []
    for c in cands:
        m = stamped_re.match(str(c.get("name", "")))
        if m:
            stamped.append((int(m.group(1)), c))
    if stamped:
        return max(stamped, key=lambda pair: pair[0])[1]
    for ext in (".jpg", ".png", ".j"):
        for c in cands:
            if str(c.get("name", "")).lower().endswith(ext):
                return c
    return cands[0]


def whatsapp_profile_photo(
    source_id: str, number: str, dest_dir: str
) -> dict[str, Any]:
    """Baixa a foto de perfil cacheada de um número do WhatsApp (dono ou interlocutor).

    Busca o avatar em `ufed:Name:<número>*` (iPhone: Media/Profile e
    spotlight; Android: Avatars) e grava o arquivo em dest_dir (binário nunca
    entra em contexto). 0 resultados ≠ número inexistente: o número pode
    existir em From/To sem avatar cacheado — checar número parecido antes de
    concluir.

    Args:
        source_id: source no IPED (veja iped_list_sources).
        number: número completo em dígitos (ex.: 5511999999999; +55 e espaços são normalizados).
        dest_dir: pasta de destino (recomendado: caminho absoluto; criada se não existir).
    """
    num = _require_number(number)
    query = build_profile_query(num)
    ids = search_ids(query, source_id, _WA_HINT)
    if not ids:
        return {
            "source": source_id,
            "found": False,
            "number": num,
            "hint": (
                "nenhum avatar cacheado para este número; ele pode existir em "
                "From/To sem foto (validado no dono Android). Checar número "
                "parecido antes de concluir."
            ),
        }
    candidates = []
    for doc_id, props in fetch_docs_parallel(source_id, ids):
        if not props:
            continue
        name = _first(props.get("name"))
        size = _first(props.get("size"))
        candidates.append(
            {"doc_id": doc_id, "name": str(name) if name else "", "size": size}
        )
    chosen = select_profile_photo(candidates, num)
    if chosen is None:
        return {
            "source": source_id,
            "found": False,
            "number": num,
            "hint": "só há miniaturas (.thumb) cacheadas; nenhum avatar em tamanho cheio.",
        }
    result = doc_export(source_id, chosen["doc_id"], dest_dir)
    if result.get("no_file"):
        return {
            "source": source_id,
            "found": False,
            "number": num,
            "hint": f"doc {chosen['doc_id']} ({chosen['name']}) sem arquivo baixável.",
        }
    return {
        "source": source_id,
        "found": True,
        "number": num,
        "doc_id": chosen["doc_id"],
        "name": chosen["name"],
        "size": result.get("size"),
        "path": result.get("path"),
    }


def whatsapp_conversation(
    source_id: str, number: str, granularity: str = "month"
) -> dict[str, Any]:
    """Analisa uma conversa privada do WhatsApp: volume total, período, intensidade por mês/semana/dia e divisão enviado/recebido.

    `sent` = mensagens do dono para o interlocutor (To); `received` = do
    interlocutor para o dono (From); `no_direction` = eventos de sistema sem
    From/To. granularity=month devolve a série completa; week/day devolvem os
    top-10 buckets. Número não encontrado → found=false (pode ser conversa de
    grupo — Fase 4).

    Args:
        source_id: source no IPED (veja iped_list_sources).
        number: número do interlocutor (ex.: 5511999999999; +55 e espaços são normalizados).
        granularity: month (padrão) | week | day.
    """
    if granularity not in ("month", "week", "day"):
        raise ValueError("granularity deve ser month, week ou day.")
    num = _require_number(number)
    records, _sha_map, dropped = collect_messages(source_id)
    conv = [r for r in records if r.kind == "chat" and r.key == num]
    if not conv:
        return {
            "source": source_id,
            "number": num,
            "found": False,
            "hint": (
                "nenhuma conversa privada com este número; checar número "
                "parecido (whatsapp_overview lista os interlocutores) — pode "
                "ser conversa de grupo (Fase 4)."
            ),
        }
    sent = sum(1 for r in conv if r.to_number == num)
    received = sum(1 for r in conv if r.from_number == num)
    dates = sorted(r.date for r in conv if r.date)
    buckets = Counter(bucket_key(r.date, granularity) for r in conv if r.date)
    if granularity == "month":
        series = [
            {"bucket": b, "messages": c} for b, c in sorted(buckets.items())
        ]
    else:
        series = [
            {"bucket": b, "messages": c} for b, c in buckets.most_common(10)
        ]
    return {
        "source": source_id,
        "number": num,
        "found": True,
        "name": _display_name_for(records, num),
        "messages": len(conv),
        "dropped_docs": dropped,
        "sent": sent,
        "received": received,
        "no_direction": len(conv) - sent - received,
        "period": {"start": dates[0], "end": dates[-1]} if dates else None,
        "intensity": {"granularity": granularity, "buckets": series},
    }


def whatsapp_conversation_window(
    source_id: str, number: str, start: str, end: str
) -> dict[str, Any]:
    """Analisa uma conversa privada do WhatsApp dentro de uma janela de tempo: total de mensagens e segmentação por tipo (texto, áudio, imagem, vídeo, PDF).

    Datas em ISO 8601 (AAAA-MM-DD[THH:MM[:SS]][Z]) ou DD/MM/YYYY[ HH:MM[:SS]];
    data-only no fim cobre o dia inteiro (T23:59:59Z). `sample_ids` dá até 5
    docs para puxar conteúdo via iped_doc_info/iped_doc_text/iped_doc_export.

    Args:
        source_id: source no IPED (veja iped_list_sources).
        number: número do interlocutor (ex.: 5511999999999).
        start: início da janela.
        end: fim da janela.
    """
    num = _require_number(number)
    start_iso, end_iso = normalize_range(start, end)
    base = build_conversation_window_query(num, start_iso, end_iso)
    queries = {
        "total": base,
        "audio": f"{base} AND {build_marker_query('audio')}",
        "image": f"{base} AND {build_marker_query('image')}",
        "video": f"{base} AND {build_marker_query('video')}",
        "pdf": f"{base} AND {build_media_mime_query('application/pdf')}",
    }

    def _count(key: str) -> tuple[str, list[int]]:
        return key, search_ids(queries[key], source_id, _WA_HINT)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = dict(pool.map(_count, queries))
    total_ids = results["total"]
    total = len(total_ids)
    audio = len(results["audio"])
    image = len(results["image"])
    video = len(results["video"])
    pdf = len(results["pdf"])
    return {
        "source": source_id,
        "number": num,
        "window": {"start": start_iso, "end": end_iso},
        "total": total,
        "by_type": {
            "text": total - audio - image - video - pdf,
            "audio": audio,
            "image": image,
            "video": video,
            "pdf": pdf,
        },
        "sample_ids": total_ids[:5],
    }


def whatsapp_export_conversation(
    source_id: str,
    number: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 500,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Exporta as mensagens de uma conversa privada do WhatsApp para um arquivo JSON local, com texto, transcrição de áudio e texto de PDF anexado.

    Cada mensagem sai com id, date, text (Message-Body), audio_transcription
    (do doc de mídia via linkedItems) e attachment_text (texto extraído do
    PDF anexado via /text). Imagem → null (sem OCR no escopo); PDF Android
    não tem linkedItems → null. O JSON é gravado em output_dir (ou
    $IPED_MCP_EXPORTS_DIR, ou /tmp/iped_mcp/exports) e o caminho absoluto é
    devolvido (conteúdo nunca entra em contexto).

    Args:
        source_id: source no IPED (veja iped_list_sources).
        number: número do interlocutor (ex.: 5511999999999).
        start: início da janela opcional (ISO 8601 ou DD/MM/YYYY[ HH:MM[:SS]]).
        end: fim da janela opcional (mesmos formatos; start e end juntos).
        limit: máximo de mensagens (padrão 500, teto 500).
        output_dir: pasta de destino do JSON (recomendado: caminho absoluto;
            sem o parâmetro, grava em /tmp/iped_mcp/exports).
    """
    if not 1 <= limit <= 500:
        raise ValueError("limit deve estar entre 1 e 500.")
    num = _require_number(number)
    window: tuple[str, str] | None = None
    if start or end:
        if not (start and end):
            raise ValueError("'start' e 'end' devem ser informados juntos.")
        window = normalize_range(start, end)
    records, _sha_map, dropped = collect_messages(source_id)
    conv = [r for r in records if r.kind == "chat" and r.key == num]
    if not conv:
        return {
            "source": source_id,
            "number": num,
            "found": False,
            "hint": "nenhuma conversa privada com este número (ver whatsapp_overview).",
        }
    if window:
        w_start, w_end = window
        conv = [r for r in conv if r.date and w_start <= r.date <= w_end]
    conv.sort(key=lambda r: r.date or "")
    selected = conv[:limit]
    fetched = fetch_docs_parallel(source_id, [r.doc_id for r in selected])
    hashes: list[str] = []
    for rec in selected:
        for h in rec.linked_hashes:
            if h not in hashes:
                hashes.append(h)
    media = _fetch_media(source_id, hashes)
    messages = []
    for rec, (_doc_id, props) in zip(selected, fetched):
        body = _first(props.get("Message-Body")) if props else None
        date = _first(props.get("Communication:Date")) if props else rec.date
        transcription: str | None = None
        attachment_text: str | None = None
        for h in rec.linked_hashes:
            entry = media.get(h)
            if not entry:
                continue
            if entry["transcription"] is not None:
                transcription = entry["transcription"]
            if entry["attachment_text"] is not None:
                attachment_text = entry["attachment_text"]
        messages.append(
            {
                "id": rec.doc_id,
                "date": str(date) if date is not None else None,
                "text": str(body) if body is not None else None,
                "audio_transcription": transcription,
                "attachment_text": attachment_text,
            }
        )
    target, is_abs = _prepare_export_dir(output_dir)
    now = datetime.now()
    path = os.path.abspath(
        os.path.join(target, f"whatsapp_{source_id}_{num}_{now:%Y%m%d-%H%M%S}.json")
    )
    payload = {
        "source": source_id,
        "number": num,
        "window": {"start": window[0], "end": window[1]} if window else None,
        "generated_at": now.isoformat(timespec="seconds"),
        "exported": len(messages),
        "messages": messages,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    result: dict[str, Any] = {
        "source": source_id,
        "number": num,
        "path": path,
        "exported": len(messages),
        "total_in_conversation": len(conv),
        "dropped_docs": dropped,
    }
    if output_dir is None:
        result["warning"] = (
            f"JSON gravado no diretório padrão {target}. "
            "Para gravar no seu projeto, chame novamente passando output_dir "
            "com o caminho absoluto da pasta exports/."
        )
    elif not is_abs:
        result["warning"] = (
            f"output_dir '{output_dir}' é relativo e resolve contra o cwd do "
            "servidor (não do seu projeto). Use um caminho absoluto."
        )
    return result


def whatsapp_text_search(source_id: str, term: str, top: int = 10) -> dict[str, Any]:
    """Busca um termo em todas as conversas privadas do WhatsApp, em três camadas: corpo da mensagem, transcrição de áudio e texto de PDF anexado.

    `conversations` conta conversas distintas com ≥1 hit por camada (a soma
    das camadas pode exceder `total`, que é a união). `top` ranqueia as
    conversas pelo nº total de hits. A camada de anexo itera todos os PDFs do
    source (lenta em sources grandes, ~1 min); mensagem e áudio são rápidas.
    O termo casa por token Lucene em mensagem/áudio e por substring
    case-insensitive em PDF.

    Args:
        source_id: source no IPED (veja iped_list_sources).
        term: termo a buscar (ex.: pix).
        top: nº de conversas a devolver (padrão 10).
    """
    if not term or not term.strip():
        raise ValueError("'term' não pode ser vazio.")
    if top < 1:
        raise ValueError("top deve ser >= 1.")
    term = term.strip()
    records, sha_map, dropped = collect_messages(source_id)
    msg_hits: Counter = Counter()
    msg_ids = search_ids(build_message_term_query(term), source_id, _WA_HINT)
    for _doc_id, props in fetch_docs_parallel(source_id, msg_ids):
        if not props:
            continue
        name = str(_first(props.get("name")) or "")
        m = PRIVATE_NAME_RE.match(name)
        if m:
            msg_hits[m.group(1)] += 1
    aud_hits: Counter = Counter()
    aud_ids = search_ids(build_audio_term_query(term), source_id, _WA_HINT)
    for _doc_id, props in fetch_docs_parallel(source_id, aud_ids):
        if not props:
            continue
        for key in sha_map.get(_media_hash(props) or "") or ():
            aud_hits[key] += 1
    att_hits: Counter = Counter()
    pdf_ids = search_ids(build_pdf_query(), source_id, _WA_HINT)
    term_lower = term.lower()
    pdf_targets: list[tuple[int, tuple[str, ...]]] = []
    for doc_id, props in fetch_docs_parallel(source_id, pdf_ids):
        if not props:
            continue
        keys = tuple(sha_map.get(_media_hash(props) or "") or ())
        if keys:
            pdf_targets.append((doc_id, keys))

    def _pdf_hit(item: tuple[int, tuple[str, ...]]) -> tuple[tuple[str, ...], bool]:
        doc_id, keys = item
        text = get_document_text(source_id, doc_id)
        return keys, bool(text) and term_lower in text.lower()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for keys, hit in pool.map(_pdf_hit, pdf_targets):
            if hit:
                for key in keys:
                    att_hits[key] += 1
    all_keys = set(msg_hits) | set(aud_hits) | set(att_hits)
    ranked = [
        {
            "number": key,
            "name": _display_name_for(records, key),
            "message": msg_hits[key],
            "audio": aud_hits[key],
            "attachment": att_hits[key],
            "total": msg_hits[key] + aud_hits[key] + att_hits[key],
        }
        for key in all_keys
    ]
    ranked.sort(key=lambda e: -e["total"])
    return {
        "source": source_id,
        "term": term,
        "dropped_docs": dropped,
        "conversations": {
            "message": len(msg_hits),
            "audio": len(aud_hits),
            "attachment": len(att_hits),
            "total": len(all_keys),
        },
        "top": ranked[:top],
    }


def whatsapp_reference() -> dict[str, Any]:
    """Referência validada de campos, padrões de name, marcadores de tipo e queries canônicas do WhatsApp no IPED, com as armadilhas conhecidas.

    Use em dúvida sobre um campo ou query antes de montar buscas livres
    (iped_search). Sem chamada ao IPED (conteúdo estático, derivado dos
    testes validados das Fases 1–3).
    """
    return {
        "canonical_filters": {
            "all_whatsapp": "accountType:WhatsApp",
            "messages": 'accountType:WhatsApp AND category:"Instant Messages"',
            "group_messages": "isGroupMessage:true",
            "private_messages": 'accountType:WhatsApp AND category:"Instant Messages" AND -isGroupMessage:true',
            "private_conversation": 'accountType:WhatsApp AND category:"Instant Messages" AND -isGroupMessage:true AND name:Chat AND name:<número>',
            "date_range": "Communication\\:Date:[A TO B] (superior date-only → T23:59:59Z)",
            "account": 'contentType:"application/x-whatsapp-account"',
            "contacts": "accountType:WhatsApp AND name:Contact",
            "media_by_hash": "ufed\\:SHA256:<HASH>",
            "audio_transcription": "audio\\:transcription:<termo>",
            "pdf_with_text": 'category:"PDF Documents" AND size:[1 TO *]',
        },
        "name_patterns": {
            "private": "WhatsApp Chat - <contato> - <número>_message_<n> (iPhone: <número>_0_message_<n>; Android sem nome: WhatsApp Chat - <número>_message_<n>)",
            "group": "WhatsApp Group - <grupo> - <jid>_message_<n> (Android: <jid>_0_message_<n>)",
            "channel": "WhatsApp Channel - <nome> - <jid>_message_<n>",
            "status": "WhatsApp Status - <nome> - <número>_message_<n> (só iPhone)",
            "account": "WhatsApp Account: <nome>",
            "contact": "WhatsApp Contact: <nome>",
            "private_key_regex": r"^WhatsApp Chat - (?:.+ - )?(\d+)(?:_\d+)?_message_\d+$",
        },
        "type_markers": {
            "audio": 'Message-Body:"! AUDIO_MESSAGE"',
            "image": 'Message-Body:"! IMAGE_MESSAGE"',
            "video": 'Message-Body:"! VIDEO_MESSAGE"',
            "pdf": "mediaMime:application/pdf",
            "note": "mediaMime só com valor exato (sem wildcard) e é multi-valorado (jpeg=webp=png — não somar).",
        },
        "from_to_formats": [
            "Nome (5511999999999)",
            "Nome (5511999999999@s.whatsapp.net)",
            "5511999999999@s.whatsapp.net (JID puro)",
            "ausente (eventos de sistema)",
        ],
        "pitfalls": {
            "F1.1": "campo com ':' no nome sem escape (Communication:From, audio:transcription, ufed:SHA256) → HTTP 500; usar Communication\\:From etc.",
            "F1.2": "name é tokenizado: name:WhatsApp*message* → 0; usar tokens exatos (name:Chat AND name:<número>).",
            "F1.3": "isGroupMessage só existe quando true: isGroupMessage:false → 0.",
            "F1.4": "Communication:To:Group não é confiável p/ grupo (ex.: 1.489 vs 6.055 do isGroupMessage:true).",
            "F1.5": "mediaMime sem wildcard: mediaMime:image/* → 0; usar valor exato (mediaMime:image/jpeg).",
            "F1.6": "transcrição de áudio fica no doc de mídia (audio:transcription), não na mensagem.",
            "F1.7": "accountType:WhatsApp inclui não-mensagens (conta, contatos, status, chamadas).",
            "F1.8": "volume alto (10⁴–10⁵ docs): contagem + amostra, nunca a lista cheia.",
            "F1.9": "negação Lucene: X -Y AND Z descarta X; usar X AND -Y AND Z, (X -Y) AND Z ou NOT Y.",
            "F1.10": "@ em valor sem aspas → HTTP 500; usar prefixo numérico (Name:<número>*).",
            "F1.11": "contentType sem aspas → misparse (1.499.455 docs); valor entre aspas.",
            "F1.12": "name:WhatsApp é superset (+imagens de perfil, pastas, logs).",
            "F1.13": "mediaMime multi-valorado (jpeg=webp=png) — não somar.",
            "F1.14": "contentType não é filtro confiável de mensagem (message/x-whatsapp-message < IM).",
            "F1.15": "nome de grupo Android com segmento extra: <jid>_0_message_<n>.",
            "F1.16": "PDF Android não tem linkedItems (sem link mensagem→mídia).",
            "F1.17": "link mensagem→mídia é unidirecional (linkedItems sha-256 → ufed\\:SHA256).",
            "F1.18": "min/max por amostra não é o real; usar coleta completa ou busca binária.",
            "F2.1": "WhatsApp Channels: isGroupMessage:true INCLUI canais; separar grupo×canal só pelo prefixo do name.",
            "F2.2": "chat privado iPhone com segmento extra _N: <número>_0_message_<n>.",
            "F2.3": "chat privado Android sem nome: WhatsApp Chat - <número>_message_<n>.",
            "F2.4": "From/To em 4 formatos (Nome (número) · Nome (JID) · JID puro · ausente).",
            "F2.5": "msg de grupo sem isGroupMessage (eventos de sistema): contagem por flag subconta; usar name-based.",
            "F2.6": "range de data date-only no fim trunca o último dia; usar T23:59:59Z.",
            "F2.7": "@ em valor da query → HTTP 500; usar prefixo numérico Name:<número>*.",
            "F2.8": "isGroupMessage:true também em chamadas de grupo e 1 contato (só Android); p/ msgs: IM AND isGroupMessage:true.",
            "F2.9": "artefato WhatsApp Chat - 0_message_N (ex.: 4 docs From:'0'); excluir da contagem real.",
            "F2.10": "self-chat do dono (Mensagens salvas): entra nas conversas privadas, não vira interlocutor.",
            "F2.11": "name:<número> casa Status/Chamadas (+1/+poucos); usar name:Chat AND name:<número>.",
            "F3.1": "doc_text não é buscável via /search; texto de anexo só iterando /text nos PDFs.",
            "F3.2": "filtro privado bruto inclui eventos de grupo/canal sem a flag; refinar por name prefix WhatsApp Chat - .",
            "F3.3": "token Lucene ≠ substring (Message-Body:pix ≠ pixeado); a query Lucene é a canônica.",
            "F3.4": "PDF vazio (size=0) tem /text vazio; filtrar size:[1 TO *].",
            "F3.5": "imagem tem /text vazio (só quebras de linha); attachment_text só útil p/ PDF/documento.",
            "F3.6": "name:<número> casa Status; usar name:Chat AND name:<número> p/ conversa privada exata.",
        },
    }


def register(mcp: FastMCP) -> None:
    mcp.add_tool(whatsapp_overview)
    mcp.add_tool(whatsapp_account)
    mcp.add_tool(whatsapp_profile_photo)
    mcp.add_tool(whatsapp_conversation)
    mcp.add_tool(whatsapp_conversation_window)
    mcp.add_tool(whatsapp_export_conversation)
    mcp.add_tool(whatsapp_text_search)
    mcp.add_tool(whatsapp_reference)
