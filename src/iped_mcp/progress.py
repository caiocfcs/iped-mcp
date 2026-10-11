"""Envelope de contadores para operações longas (DESIGN 09 / M-9).

Operações de minutos (scan regex, coleta WhatsApp, downloads em massa)
deixam de ser mudas: cada tool longa devolve, gerado por ESTE módulo, o
envelope único {scanned, processed, phase, elapsed_s, remaining_est} e —
quando a tool aceita fatiamento — a janela `batch` efetiva da chamada,
para o agente distinguir "trabalhando" de "travado" e continuar de onde
parou (D-21/G1).

Progress tokens do protocolo MCP não são usados nesta versão: a
verificação de fonte do fastmcp instalado (ver
research/reports/phase-7-m9-feedback.md) mostra que `ctx.report_progress`
só notifica quando o cliente envia `progressToken` na chamada e exige
tools `async` — canal não verificável sem executar o servidor. O envelope
na resposta funciona com qualquer cliente MCP (fallback (a)+(b) do design,
que o próprio design declara suficiente).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

# Fase do envelope: "partial" = lote devolvido com domínio pela frente
# (o agente pode reinvocar com offset = offset + limit); "complete" = a
# chamada cobriu o domínio até o fim (lote único ou último lote).
PHASE_COMPLETE = "complete"
PHASE_PARTIAL = "partial"

# Casas decimais dos campos de tempo (segundos) no envelope.
_TIME_DECIMALS = 3


@dataclass(frozen=True)
class BatchWindow:
    """Janela de lote resolvida sobre um domínio de `total` itens.

    Attributes:
        total: tamanho do domínio (todos os itens da busca).
        offset: itens pulados antes do lote (como pedido).
        limit: tamanho pedido do lote (None = até o fim do domínio).
        start: índice inicial efetivo (clampado em `total`).
        end: índice final efetivo (exclusivo, clampado em `total`).
        has_more: True quando restam itens após `end` no domínio.
    """

    total: int
    offset: int
    limit: int | None
    start: int
    end: int
    has_more: bool

    @property
    def scanned(self) -> int:
        """Quantidade de itens efetivamente no lote (end - start)."""
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        """Representação da janela para a chave `batch` do envelope."""
        return {
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "has_more": self.has_more,
        }


def resolve_batch(
    total: int, offset: int = 0, limit: int | None = None
) -> BatchWindow:
    """Valida e resolve (offset, limit) sobre um domínio de `total` itens.

    O default (offset=0, limit=None) cobre o domínio inteiro: mantém o
    comportamento 1-shot atual das tools (mitigação de risco do DESIGN 09
    — lotes só quando o agente pede).

    Args:
        total: tamanho do domínio (>= 0).
        offset: itens a pular antes do lote.
        limit: itens no lote (None = até o fim).

    Returns:
        A BatchWindow efetiva (índices clampados em `total`).

    Raises:
        ValueError: offset negativo ou limit < 1.
    """
    if offset < 0:
        raise ValueError("offset deve ser >= 0.")
    if limit is not None and limit < 1:
        raise ValueError("limit deve ser >= 1 (ou omitido para o domínio todo).")
    start = min(offset, total)
    end = total if limit is None else min(start + limit, total)
    return BatchWindow(
        total=total,
        offset=offset,
        limit=limit,
        start=start,
        end=end,
        has_more=end < total,
    )


def op_envelope(
    started_at: float,
    *,
    scanned: int,
    processed: int,
    batch: BatchWindow | None = None,
) -> dict[str, Any]:
    """Monta o envelope de feedback de uma operação longa (helper único, AC5).

    Args:
        started_at: valor de `time.monotonic()` capturado no início da
            operação (a duração medida inclui acerto de cache — ~0 s diz
            ao agente que nada foi varrido de novo).
        scanned: itens varridos nesta chamada (docs lidos, downloads
            executados, mensagens coletadas).
        processed: itens que produziram resultado (match, arquivo salvo).
        batch: janela efetiva quando a tool aceita lotes; None em tools
            sem fatiamento (a chave `batch` não aparece no envelope).

    Returns:
        {"phase", "scanned", "processed", "elapsed_s", "remaining_est"}
        (+ "batch" quando `batch` é informado). `remaining_est` estima os
        segundos para o resto do domínio pela taxa deste lote (0.0 quando
        nada resta ou o lote não varreu nada).
    """
    elapsed = max(time.monotonic() - started_at, 0.0)
    has_more = bool(batch and batch.has_more)
    remaining = _remaining_est(elapsed, scanned, batch) if has_more else 0.0
    envelope: dict[str, Any] = {
        "phase": PHASE_PARTIAL if has_more else PHASE_COMPLETE,
        "scanned": scanned,
        "processed": processed,
        "elapsed_s": round(elapsed, _TIME_DECIMALS),
        "remaining_est": round(remaining, _TIME_DECIMALS),
    }
    if batch is not None:
        envelope["batch"] = batch.to_dict()
    return envelope


def _remaining_est(elapsed: float, scanned: int, batch: BatchWindow) -> float:
    """Estima segundos até cobrir o domínio: taxa do lote x itens restantes.

    Args:
        elapsed: segundos gastos no lote atual.
        scanned: itens varridos no lote (0 → sem taxa, estimativa 0).
        batch: janela efetiva com `total`/`end` do domínio.

    Returns:
        Estimativa em segundos (sempre >= 0).
    """
    if scanned <= 0:
        return 0.0
    return elapsed * (batch.total - batch.end) / scanned
