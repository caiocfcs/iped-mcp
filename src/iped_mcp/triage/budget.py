"""Orçamento de GET internos das tools de triagem (R-TRIAGE-1).

Módulo próprio exigido pelo DESIGN 09-B §2.3 ("HttpBudget em
triage/budget.py — uma responsabilidade por módulo"): a classe de
contador, o fetch de props limitado pelo orçamento e o warning canônico
de estouro. Nascido privado em ``identity.py`` na onda B-1; extraído no
retrofit R-TRIAGE-1 (fase 9 QA-fecho) SEM mudança de contrato —
``identity`` reexporta os mesmos nomes por compatibilidade.

Política (§2, GO-COM-RESSALVAS no veredito do CICLO 2): estouro NUNCA é
exceção e NUNCA é silencioso — o fluxo para de emitir GET novos, fecha a
agregação com o que tem, ``phase`` vira ``partial``,
``truncated.http_budget_exceeded`` marca o corte e o warning nomeia cap,
contagem e o que ficou de fora. Tetos por tool em
:data:`knowledge.TOOL_BUDGETS` (default ``DEFAULT_MAX_INTERNAL_GETS``).
"""

from __future__ import annotations

from iped_mcp import fetch
from iped_mcp.triage import knowledge

# O que ficou de fora quando o cap foi tingido (textos do warning
# canônico; movidos de identity.py no retrofit R-TRIAGE-1).
BUDGET_OMITTED_DOCS = (
    "{count} documentos ficaram sem varredura de propriedades (cap "
    "tingido no lote de GETs /docs)"
)
BUDGET_OMITTED_QUERIES = "demais categorias não consultadas"


class HttpBudget:
    """Orçamento de GET internos desta chamada (DESIGN 09-B §2.3).

    Attributes:
        cap: teto efetivo da tool (``knowledge.TOOL_BUDGETS``).
        used: GETs realmente emitidos até aqui (/search e /docs contam 1;
            acerto de cache NÃO conta — tráfego, não trabalho lógico).
        exceeded: True quando o fluxo parou por atingir o cap (nunca é
            exceção nem silencioso: vira ``truncated`` + warning).
    """

    def __init__(self, cap: int) -> None:
        """Inicializa com o teto efetivo da tool.

        Args:
            cap: nº máximo de GET internos permitido nesta chamada.
        """
        self.cap = cap
        self.used = 0
        self.exceeded = False

    def allows(self, cost: int = 1) -> bool:
        """Diz se ainda cabem ``cost`` GETs no orçamento.

        Args:
            cost: requisições que o chamador pretende emitir.

        Returns:
            True quando ``used + cost`` não ultrapassa o cap.
        """
        return self.used + cost <= self.cap

    def record(self, cost: int = 1) -> None:
        """Conta ``cost`` GETs emitidos (tráfego real, não trabalho lógico).

        Args:
            cost: requisições efetivamente emitidas.

        Returns:
            Nada.
        """
        self.used += cost


def fetch_within_budget(
    sid: str, ids: list[int], budget: HttpBudget, omitted: list[int]
) -> list[tuple[int, dict | None]]:
    """Varre as props respeitando o GET restante do orçamento (§2.3).

    Args:
        sid: source resolvido.
        ids: candidatos na ordem de descoberta.
        budget: orçamento de GET (mutado; cada /docs conta 1).
        omitted: lista de saída; recebe a QUANTIDADE omitida num ÚNICO
            elemento (``omitted[0]`` — contrato lido por
            :func:`budget_warnings`).

    Returns:
        Pares ``(doc_id, properties|None)`` dos docs efetivamente buscados.
    """
    remaining = max(budget.cap - budget.used, 0)
    if len(ids) > remaining:
        budget.exceeded = True
        omitted.append(len(ids) - remaining)
        ids = ids[:remaining]
    pairs = fetch.fetch_docs_parallel(sid, ids)
    budget.record(len(ids))
    return pairs


def budget_warnings(budget: HttpBudget, docs_omitted: list[int]) -> list[str]:
    """Aviso canônico de estouro do orçamento (§2.3; nunca exceção).

    Args:
        budget: orçamento da chamada.
        docs_omitted: lista-contrato com a quantidade de docs sem props
            (``docs_omitted[0]``; vazia = parou nas queries).

    Returns:
        Warning com cap, contagem e o que ficou de fora; vazio sem estouro.
    """
    if not budget.exceeded:
        return []
    omitted = (
        BUDGET_OMITTED_DOCS.format(count=docs_omitted[0])
        if docs_omitted
        else BUDGET_OMITTED_QUERIES
    )
    return [
        knowledge.HTTP_BUDGET_WARNING_TEMPLATE.format(
            calls=budget.used, cap=budget.cap, omitted=omitted
        )
    ]
