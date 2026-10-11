"""Tool ``transcripts_generate`` — arquivo de transcrições ASR embutidas.

Porta a geração do artefato de transcrições do ICARO
(iped-icaro/transcripts/transcript_generator_service.py): varre as queries
canônicas de áudio, mantém os docs com o metadado ``audio:transcription``,
deduplica por hash, ordena no tempo e materializa o arquivo de blocos no
disco de exports (padrão M-10: nome UTC/source, no-clobber). CONTRATO DE
CONTEXTO (DESIGN 09 §3.4): o texto completo NUNCA vai na resposta MCP — a
resposta traz metadados + preview de ``PREVIEW_CHARS`` sempre marcado.
Nenhum LLM e nenhum Whisper: só se lê o que o IPED já transcreveu. A
varredura roda sob o orçamento R-TRIAGE-1 (``counters.http_calls``;
estouro → partial + warning, nunca exceção).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP

from iped_mcp import download, fetch
from iped_mcp.progress import PHASE_PARTIAL, op_envelope
from iped_mcp.router import get_router
from iped_mcp.triage.budget import (
    HttpBudget as _Budget,
    budget_warnings as _budget_warnings,
    fetch_within_budget as _fetch_within_budget,
)
from iped_mcp.triage.fields import first_str
from iped_mcp.triage import knowledge

_SEARCH_HINT = "transcripts_generate (varredura de áudios)"
_EMPTY_SID_MESSAGE = "'source_id' é obrigatório (veja iped_list_sources)."
_NO_DATE_KEY = (knowledge.NO_DATE_SORT_RANK, 0)


@dataclass(frozen=True)
class _AudioRecord:
    """Áudio transcrito pronto para virar bloco no arquivo (service:217-228).

    Attributes:
        name: nome do arquivo de áudio.
        path: caminho do índice com o separador normalizado para "/".
        hash: hash do doc (chave de dedup, minúsculo via chamada).
        text: transcrição colapsada em 1 linha (service:195-198).
        created_key: chave temporal de ordenação (service:200-215).
    """

    name: str
    path: str
    hash: str
    text: str
    created_key: tuple[int, int]


@dataclass
class _Sweep:
    """Contadores e avisos da varredura de áudios (envelope §3.4)."""

    audio_docs: int = 0
    with_transcript: int = 0
    without_transcript: int = 0
    duplicates: int = 0
    docs_fetched: int = 0
    scan_cap_applied: bool = False
    warnings: list[str] = field(default_factory=list)


def _created_sort_key(props: dict[str, Any]) -> tuple[int, int]:
    """Chave temporal do doc (service:200-215): com data vem antes.

    Args:
        props: propriedades do doc no envelope do jar.

    Returns:
        ``(0, epoch_segundos)`` com data reconhecida (dd/mm/aaaa HH:MM:SS
        ou dígitos puros — segundos, ou milissegundos a partir de
        ``EPOCH_MS_MIN_LEN`` dígitos); ``(1, 0)`` sem data.
    """
    raw = first_str(props, knowledge.AUDIO_CREATED_FIELDS)
    if not raw:
        return _NO_DATE_KEY
    match = re.search(knowledge.CREATED_DTL_RE, raw)
    if match:
        try:
            parsed = datetime.strptime(
                f"{match.group(1)} {match.group(2)}", knowledge.CREATED_DT_FORMAT
            )
            return knowledge.WITH_DATE_SORT_RANK, int(
                parsed.replace(tzinfo=timezone.utc).timestamp()
            )
        except ValueError:
            pass
    if raw.isdigit():
        seconds = int(raw) / 1000 if len(raw) >= knowledge.EPOCH_MS_MIN_LEN else int(raw)
        return knowledge.WITH_DATE_SORT_RANK, int(seconds)
    return _NO_DATE_KEY


def _audio_record(props: dict[str, Any], text: str, hash_value: str) -> _AudioRecord:
    """Monta o registro de um áudio transcrito (campos do service:155-160).

    Args:
        props: propriedades do doc.
        text: transcrição crua (não vazia).
        hash_value: hash resolvido (pode ser vazio — chave cai no nome).

    Returns:
        Registro com nome/path normalizados e texto em 1 linha.
    """
    return _AudioRecord(
        name=first_str(props, (knowledge.NAME_FIELD,)).strip(),
        path=first_str(props, (knowledge.PATH_FIELD,)).replace(
            knowledge.PATH_TOKEN, knowledge.PATH_SEPARATOR
        ),
        hash=hash_value,
        text=re.sub(knowledge.WS_COLLAPSE_RE, " ", text).strip(),
        created_key=_created_sort_key(props),
    )


def _consume_pairs(
    pairs: list[tuple[int, dict | None]],
    records: list[_AudioRecord],
    seen: set[str],
    sweep: _Sweep,
) -> None:
    """Extrai os registros de um lote de docs fetcheados (service:143-160).

    Sem transcrição: conta ``without_transcript`` e segue (service:147-149).
    Duplicata (hash minúsculo, fallback nome): conta ``duplicates`` e segue
    (service:150-154).

    Args:
        pairs: ``(doc_id, properties|None)`` na ordem dos ids.
        records: registros acumulados (mutado).
        seen: chaves de dedup já vistas (mutado).
        sweep: contadores da varredura (mutado).
    """
    for _, props in pairs:
        if props is None:
            continue
        sweep.audio_docs += 1
        text = first_str(props, (knowledge.TRANSCRIPT_FIELD,))
        if not text:
            sweep.without_transcript += 1
            continue
        hash_value = first_str(props, knowledge.AUDIO_HASH_FIELDS)
        key = (hash_value or first_str(props, (knowledge.NAME_FIELD,))).lower()
        if key in seen:
            sweep.duplicates += 1
            continue
        seen.add(key)
        records.append(_audio_record(props, text, hash_value))


def _collect_records(
    sid: str, sweep: _Sweep, budget: _Budget, docs_omitted: list[int]
) -> list[_AudioRecord]:
    """Varre as queries candidatas até a primeira produzir registros.

    Fallback só quando a query anterior não produziu nada
    (service:165-166); query que falha vira warning e o fallback segue
    (tolerância do service:167-170). O orçamento R-TRIAGE-1 (retrofit,
    DESIGN 09-B §2.3) encerra a varredura no cap sem exceção: o artefato
    sai com o que foi coletado e o warning/parcial são marcados no
    envelope.

    Args:
        sid: source já resolvido (M-2).
        sweep: contadores da varredura (mutado).
        budget: orçamento de GET (mutado; /search e /docs contam 1).
        docs_omitted: saída do contrato fetch_within_budget (quantidade
            em [0]) — docs que não couberam no orçamento.

    Returns:
        Registros dos áudios transcritos (ordem de varredura).
    """
    records: list[_AudioRecord] = []
    seen: set[str] = set()
    for query in knowledge.AUDIO_QUERIES:
        if not budget.allows():
            budget.exceeded = True
            break
        try:
            ids = fetch.search_ids(query, sid, hint=_SEARCH_HINT)
        except ValueError as exc:
            sweep.warnings.append(f"query {query} falhou: {exc}")
            budget.record()
            continue
        budget.record()
        if len(ids) > knowledge.AUDIO_SCAN_MAX_DOCS:
            ids = ids[: knowledge.AUDIO_SCAN_MAX_DOCS]
            sweep.scan_cap_applied = True
        pairs = _fetch_within_budget(sid, ids, budget, docs_omitted)
        sweep.docs_fetched += sum(1 for _, props in pairs if props is not None)
        _consume_pairs(pairs, records, seen, sweep)
        if records:
            break
    return records


def _build_file_content(records: list[_AudioRecord]) -> str:
    """Conteúdo do artefato: blocos canônicos em ordem temporal (service:99).

    Args:
        records: registros a gravar (mutado: ordenado por ``created_key``).

    Returns:
        Texto final com um bloco "Nome:/Caminho:/HASH:/<vazia>/<texto>"
        por áudio, join ``BLOCK_JOIN`` (service:217-228).
    """
    records.sort(key=lambda record: record.created_key)
    blocks = [
        knowledge.BLOCK_TEMPLATE.format(
            name=record.name,
            path=record.path,
            hash=record.hash,
            text=record.text,
        )
        for record in records
    ]
    return knowledge.BLOCK_JOIN.join(blocks)


def _preview(content: str) -> tuple[str, bool]:
    """Preview limitado a ``PREVIEW_CHARS`` com corte SEMPRE marcado.

    Args:
        content: conteúdo completo do arquivo.

    Returns:
        ``(preview, truncated)``; truncado devolve exatamente
        ``PREVIEW_CHARS`` caracteres com o sufixo ``PREVIEW_ELLIPSIS``.
    """
    limit = knowledge.PREVIEW_CHARS
    if len(content) <= limit:
        return content, False
    return content[: limit - 1] + knowledge.PREVIEW_ELLIPSIS, True


def _zero_payload(
    sid: str, started: float, sweep: _Sweep, budget: _Budget,
    docs_omitted: list[int],
) -> dict[str, Any]:
    """Contrato zero (áudio nenhum ou nenhum transcrito): não grava nada.

    Args:
        sid: source triado.
        started: ``time.monotonic()`` do início.
        sweep: contadores da varredura.
        budget: orçamento R-TRIAGE-1 da chamada (counters.http_calls).
        docs_omitted: docs sem props por orçamento (quantidade em [0]).

    Returns:
        Envelope com ``file_path=None`` e ``message`` nomeando o metadado
        ``audio:transcription`` (service:92-97) — nunca exceção (§3.4).
    """
    return _payload(
        sid,
        started,
        sweep,
        budget,
        docs_omitted,
        file_path=None,
        with_transcript=0,
        file_bytes=0,
        preview="",
        preview_truncated=False,
        message=knowledge.TRANSCRIPTS_EMPTY_MESSAGE,
    )


def _payload(
    sid: str,
    started: float,
    sweep: _Sweep,
    budget: _Budget,
    docs_omitted: list[int],
    *,
    file_path: str | None,
    with_transcript: int,
    file_bytes: int,
    preview: str,
    preview_truncated: bool,
    message: str | None = None,
) -> dict[str, Any]:
    """Monta o envelope de resposta (§3.4: metadados, texto fora do MCP).

    Args:
        sid: source processado.
        started: ``time.monotonic()`` do início (contadores M-9).
        sweep: contadores da varredura.
        budget: orçamento R-TRIAGE-1 da chamada.
        docs_omitted: docs sem props por orçamento (quantidade em [0]).
        file_path: artefato gravado (None no contrato zero).
        with_transcript: áudios únicos com transcrição no arquivo.
        file_bytes: tamanho do artefato (0 no contrato zero).
        preview: primeiros caracteres do artefato (cortado ou não).
        preview_truncated: marca de corte do preview.
        message: texto explicativo do contrato zero (None no caso normal).

    Returns:
        Envelope JSON-serializável com ``counters`` (op_envelope M-9,
        http_calls R-TRIAGE-1) e ``truncated`` (tetos sempre marcados).
    """
    sweep.warnings.extend(_budget_warnings(budget, docs_omitted))
    payload: dict[str, Any] = {
        "source_id": sid,
        "file_path": file_path,
        "audio_docs": sweep.audio_docs,
        "with_transcript": with_transcript,
        "without_transcript": sweep.without_transcript,
        "duplicates": sweep.duplicates,
        "file_bytes": file_bytes,
        "preview": preview,
        "preview_truncated": preview_truncated,
        "counters": dict(
            op_envelope(
                started, scanned=sweep.audio_docs, processed=with_transcript
            ),
            docs_fetched=sweep.docs_fetched,
            http_calls=budget.used,
            cached=False,
        ),
        "truncated": {
            "scan_cap_applied": sweep.scan_cap_applied,
            "http_budget_exceeded": budget.exceeded,
            "http_budget_calls_at_stop": (
                budget.used if budget.exceeded else None
            ),
            # docs_omitted carrega a QUANTIDADE num único elemento
            # (contrato fetch_within_budget) — por isso sum(), não len().
            "docs_omitted_by_budget": sum(docs_omitted),
        },
        "warnings": list(sweep.warnings),
    }
    if message is not None:
        payload["message"] = message
    if budget.exceeded:
        payload["counters"]["phase"] = PHASE_PARTIAL
    return payload


def build_transcripts(
    source_id: str, dest_dir: str | None = None
) -> dict[str, Any]:
    """Núcleo da geração do arquivo de transcrições (testável sem MCP).

    Args:
        source_id: source a varrer (obrigatório).
        dest_dir: pasta de destino (None = ``config.EXPORTS_DIR``, padrão
            M-10).

    Returns:
        Envelope de ``_payload`` (shape DESIGN 09 §3.4).

    Raises:
        ValueError: source_id vazio, inexistente ou endpoint offline
            (textos estáveis da política M-2, via ``Router.resolve``).
    """
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError(_EMPTY_SID_MESSAGE)
    get_router().resolve(sid)  # M-2 antes de qualquer HTTP de busca (§2.6)
    started = time.monotonic()
    sweep = _Sweep()
    budget = _Budget(knowledge.TOOL_BUDGETS["transcripts_generate"])
    docs_omitted: list[int] = []
    records = _collect_records(sid, sweep, budget, docs_omitted)
    if not records:
        return _zero_payload(sid, started, sweep, budget, docs_omitted)
    content = _build_file_content(records)
    path = download.export_path(
        dest_dir, sid, knowledge.TRANSCRIPT_FILE_BASENAME, ext=".txt"
    )
    with open(path, "w", encoding="utf-8") as artifact:
        artifact.write(content)
    preview, truncated = _preview(content)
    return _payload(
        sid,
        started,
        sweep,
        budget,
        docs_omitted,
        file_path=path,
        with_transcript=len(records),
        file_bytes=os.path.getsize(path),
        preview=preview,
        preview_truncated=truncated,
    )


def transcripts_generate(
    source_id: str, dest_dir: str | None = None
) -> dict[str, Any]:
    """Gera o arquivo de transcrições de áudio embutidas no índice.

    Varre as queries canônicas de áudio (categoria ``Audios``/``Áudios`` e
    o fallback por extensão), mantém os docs com o metadado ASR embutido
    ``audio:transcription``, deduplica por hash e ordena no tempo (com
    data antes dos sem data). Grava o artefato ``Transcricoes.txt`` no
    disco de exports (nome UTC/source, no-clobber — o MCP não toca o caso)
    e devolve SOMENTE metadados + preview: o texto completo nunca vai na
    resposta. Caso sem áudios (ou sem nenhuma transcrição) é o contrato
    zero explícito: nada é gravado e ``message`` nomeia o metadado —
    nunca exceção.

    Args:
        source_id: source a varrer (veja iped_list_sources).
        dest_dir: pasta de destino do artefato (None = pasta de exports do
            servidor, ``IPED_MCP_EXPORTS_DIR``).

    Returns:
        {source_id, file_path, audio_docs, with_transcript,
        without_transcript, duplicates, file_bytes, preview,
        preview_truncated, counters, truncated, warnings}; ``message``
        quando não há transcrição alguma (``file_path=None``, contadores
        zerados).

    Raises:
        ValueError: source_id vazio, inexistente ou servidor indisponível
            (textos estáveis da política M-2).
    """
    return build_transcripts(source_id, dest_dir)


def register(mcp: FastMCP) -> None:
    """Registra a tool de transcrições (T-2) no servidor.

    Args:
        mcp: servidor FastMCP ativo.
    """
    mcp.add_tool(transcripts_generate)
