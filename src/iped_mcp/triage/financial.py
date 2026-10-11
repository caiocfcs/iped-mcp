"""Tool ``triage_financial`` — eixo financeiro (T-4, DESIGN 09-B §4).

Porta o motor do eixo "Financeiro" do ICARO (``financial_strategy.py`` +
handlers ``_finding_from_financial_evidence``/``_finding_from_card`` do
``draft_report_iped_manager.py``) para uma única chamada MCP determinística:
18 queries de categoria (:32-35/:38/:49) → sondas de detectores Regex por
caso (§4.3) → propriedades → classificação fiel (:40-87) → três famílias
(evidências :161-202, cartões com jumpTargets :243-334, contas/ativos
:106-159) → achados pontuados pelos handlers (:885-911; contas sem handler —
score fixo 90 por RECOMMENDATION §4.5).

Nenhum LLM, nenhum arquivo do caso: só ``GET /search`` e ``GET /docs/{id}``
com orçamento R-TRIAGE-1 (``counters.http_calls``; estouro nunca é exceção).
O boost 95 do YAML de ranking (:73-76) NÃO é portado — fonte ausente, nada
é inventado (mesma decisão §3.2). O orçamentador ``_Budget`` é reutilizado
de ``identity.py`` até a extração em ``triage/budget.py`` (R-TRIAGE-1).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from iped_mcp.fetch import search_ids
from iped_mcp.regex.queries import build_regex_query
from iped_mcp.progress import PHASE_PARTIAL, op_envelope
from iped_mcp.router import get_router
from iped_mcp.triage import knowledge
from iped_mcp.triage.fields import first_str
from iped_mcp.triage.budget import (
    HttpBudget as _Budget,
    budget_warnings as _budget_warnings,
    fetch_within_budget as _fetch_within_budget,
)

# Mensagens operacionais (padrão _SEARCH_HINT/_EMPTY_SID_MESSAGE do piloto).
_SEARCH_HINT = (
    "confira source_id (iped_list_sources); categorias do eixo podem existir "
    "com outro rótulo no caso (lista canônica: knowledge.FINANCIAL_CATEGORIES)."
)
_EMPTY_SID_MESSAGE = "source_id é obrigatório e não pode ser vazio."


@dataclass
class _FinDoc:
    """Documento com os textos lowercase do gate + path cru para as regex.

    ``name``/``path``/``category`` são minúsculos (o gate :43-45 compara em
    minúsculas); ``raw_path`` preserva o original — as regex de UUID de
    conta (:21) e de e-mail ``.ufdr`` (:22) são sensíveis a maiúsculas.
    """

    doc_id: int
    name: str
    path: str
    category: str
    raw_path: str = ""
    props: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Scan:
    """Estado da varredura T-4 (queries, caps, contadores, avisos)."""

    query_hits: dict[str, int] = field(default_factory=dict)
    query_caps: dict[str, int] = field(default_factory=dict)
    total_cap_applied: bool = False
    docs_searched: int = 0
    docs_fetched: int = 0
    classified: int = 0
    warnings: list[str] = field(default_factory=list)


def _fin_doc(doc_id: int, props: dict[str, Any]) -> _FinDoc:
    """Constrói o _FinDoc a partir do envelope de props do jar (doc no topo).

    Args:
        doc_id: id do documento.
        props: propriedades do doc (valores em lista, WI-1/P12).

    Returns:
        ``_FinDoc`` com name/path/category minúsculos (gate :43-45 usa
        ``.lower()`` no ICARO; guardamos a forma canônica uma única vez).
    """
    raw_path = first_str(props, (knowledge.PATH_FIELD,))
    return _FinDoc(
        doc_id=doc_id,
        name=first_str(props, (knowledge.NAME_FIELD,)).lower(),
        path=raw_path.lower(),
        category=first_str(props, (knowledge.CATEGORY_FIELD,)).lower(),
        raw_path=raw_path,
        props=props,
    )


def _has_regex(props: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """True quando alguma das propriedades regex tem valor não vazio (:57)."""
    for key in keys:
        values = props.get(key) or []
        if isinstance(values, str):
            values = [values]
        if any(str(v).strip() for v in values):
            return True
    return False


def _is_card_or_account(category: str) -> bool:
    """Cartão (:46, substring das categorias de cartão) ou conta (:47)."""
    return any(
        c in category for c in knowledge.FINANCIAL_CARD_CATEGORIES
    ) or knowledge.FINANCIAL_ACCOUNT_MARKER in category


def _has_financial_term(doc: _FinDoc) -> bool:
    """Alguma palavra financeira no nome ou no path minúsculos (:60)."""
    return any(
        term in doc.name or term in doc.path
        for term in knowledge.FINANCIAL_TERMS
    )


def _passes_gate(doc: _FinDoc) -> bool:
    """Gate de classificação fiel a ``financial_strategy.py:40-68``.

    Args:
        doc: documento com textos já minúsculos.

    Returns:
        ``True`` quando o doc pertence ao eixo: cartão (:46) ou conta (:47)
        passam direto; senão a categoria precisa estar em
        ``FINANCIAL_CATEGORIES`` (:52), mídia sem termo é descartada MESMO
        com regex (:61-63, anti-ruído) e o resto exige termo no nome/path
        OU regex MONEY/BOLETO (:65-68 — o BR_BANK_ACCOUNT não liga o gate).
    """
    if _is_card_or_account(doc.category):
        return True
    if not any(c in doc.category for c in knowledge.FINANCIAL_CATEGORIES):
        return False
    if _has_financial_term(doc):
        return True
    if any(e in doc.name for e in knowledge.FINANCIAL_MEDIA_EXTS):
        return False
    return _has_regex(doc.props, knowledge.FINANCIAL_GATE_REGEX_FIELDS)


def _collect_candidate_ids(sid: str, scan: _Scan, budget: _Budget) -> list[int]:
    """Sonda as 18 categorias do eixo e deduplica os ids (cap por query).

    Idêntico ao motor do piloto T-1/T-3: ``fetch.search_ids`` por
    ``category:"X"`` com ``CATEGORY_QUERY_CAP`` por query e
    ``TOTAL_CANDIDATE_CAP`` global; 500 vira -1 + warning (M-3) e cada
    /search conta no orçamento.

    Args:
        sid: source_id resolvido.
        scan: estado da varredura (mutado).
        budget: orçamento de GET (mutado).

    Returns:
        Ids únicos na ordem de descoberta; ``total_cap_applied`` marca o
        corte quando o teto global foi atingido.
    """
    out: list[int] = []
    seen: set[int] = set()
    for category in knowledge.FINANCIAL_QUERY_CATEGORIES:
        if not budget.allows():
            budget.exceeded = True
            break
        query = knowledge.FINANCIAL_QUERY_TEMPLATE.format(category=category)
        try:
            ids = search_ids(query, sid, hint=_SEARCH_HINT)
        except ValueError as exc:
            scan.query_hits[query] = -1
            scan.warnings.append(f"query {query} falhou: {exc}")
            budget.record()
            continue
        scan.query_hits[query] = len(ids)
        budget.record()
        if len(ids) > knowledge.CATEGORY_QUERY_CAP:
            scan.query_caps[query] = len(ids) - knowledge.CATEGORY_QUERY_CAP
            ids = ids[: knowledge.CATEGORY_QUERY_CAP]
        for doc_id in ids:
            if doc_id in seen:
                continue
            seen.add(doc_id)
            out.append(doc_id)
            if len(out) >= knowledge.TOTAL_CANDIDATE_CAP:
                scan.total_cap_applied = True
                return out
    return out


def _probe_regex_detectors(
    sid: str, scan: _Scan, budget: _Budget
) -> dict[str, int]:
    """Sonda os detectores Regex do caso ANTES de confiar neles (§4.3, M-7).

    Args:
        sid: source_id resolvido.
        scan: estado da varredura (mutado: warnings).
        budget: orçamento de GET (mutado; cada sonda conta 1).

    Returns:
        ``{detector: hits}`` (hits 0/erro -1) para o painel
        ``regex_availability``. Nunca lança: detector ausente é dado, não
        falha. A sonda CREDIT_CARD é INFORMATIVA: o extrator de cartões usa
        categorias + campos ufed (:243-334), nunca a propriedade regex.
    """
    availability: dict[str, int] = {}
    for name in knowledge.FINANCIAL_REGEX_PROBES:
        query = build_regex_query(name)
        if not budget.allows():
            budget.exceeded = True
            availability[name] = -1
            scan.warnings.append(f"sonda Regex:{name} pulada: orçamento estourado")
            continue
        try:
            hits = len(search_ids(query, sid, hint=_SEARCH_HINT))
        except ValueError as exc:
            hits = -1
            scan.warnings.append(f"sonda Regex:{name} falhou: {exc}")
        budget.record()
        availability[name] = hits
        if hits == 0:
            scan.warnings.append(
                knowledge.FINANCIAL_REGEX_UNAVAILABLE_WARNING.format(name=name)
            )
    return availability


def _parse_amount(text: Any) -> float:
    """Parser BR de valores portado de ``financial_strategy.py:206-241``.

    Args:
        text: valor bruto da propriedade (pode ser não-str).

    Returns:
        Valor como ``float``; ``AMOUNT_SENTINEL`` (-1.0) para não-str,
        marcadores não-numéricos, número ilegível ou sem dígitos. Regra do
        ponto: com vírgula presente tudo o ponto é milhar; sem vírgula, o
        ponto só é decimal se o último bloco NÃO tem exatamente 3 dígitos
        ("1.234" é milhar, "1.23" é decimal — literal do ICARO).
    """
    if not isinstance(text, str):
        return knowledge.AMOUNT_SENTINEL
    clean = text.strip().replace("R$", "").replace(" ", "")
    if not clean or clean in knowledge.AMOUNT_UNPARSEABLE_MARKERS:
        return knowledge.AMOUNT_SENTINEL
    match = re.search(knowledge.AMOUNT_NUMBER_RE, clean)
    if not match:
        return knowledge.AMOUNT_SENTINEL
    number = match.group(1)
    if "," in number:
        number = number.replace(".", "").replace(",", ".")
    elif "." in number:
        head, _, tail = number.rpartition(".")
        if len(tail) == knowledge.AMOUNT_THOUSANDS_BLOCK:
            number = number.replace(".", "")
    try:
        return float(number)
    except ValueError:
        return knowledge.AMOUNT_SENTINEL


def _collapse(value: str) -> str:
    """Colapsa quebras/espaços de um valor de propriedade (beneficiary :194-196)."""
    return " ".join(value.replace("\r", " ").replace("\n", " ").split()).strip()


def _prop_list(props: dict[str, Any], key: str) -> list[str]:
    """Valores da propriedade como lista de str (jar aceita lista ou escalar)."""
    values = props.get(key) or []
    if isinstance(values, str):
        values = [values]
    return [str(v) for v in values if str(v).strip()]


def _source_label(doc: _FinDoc) -> str:
    """Nome de ARQUIVO da evidência, fiel a ``_financial_source_label`` (:675-696).

    O .name do hit para e-mails costuma ser o ASSUNTO da mensagem; a ordem
    é basename(path) → basename(ufed:local_path) → name do doc.

    Args:
        doc: documento da evidência.

    Returns:
        Rótulo de arquivo; ``""`` quando nada resolve (aí o achado usa o
        fallback ``FINANCIAL_EVIDENCE_SOURCE_FALLBACK``).
    """
    for raw in (
        doc.raw_path,
        first_str(doc.props, (knowledge.FIN_EVIDENCE_LOCAL_PATH_FIELD,)),
    ):
        base = raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
        if base:
            return base
    return doc.name


def _evidence_entry(doc: _FinDoc) -> dict[str, Any] | None:
    """Monta a evidência de UM doc (escada method/amount :183-197).

    Args:
        doc: documento com alguma propriedade de detector.

    Returns:
        ``{amount, method, beneficiary, doc_id, path, source_name}`` ou
        ``None`` se nenhuma regex do doc casou (doc sem evidência).
    """
    money = _prop_list(doc.props, knowledge.FINANCIAL_REGEX_MONEY_FIELD)
    boletos = _prop_list(doc.props, knowledge.FINANCIAL_REGEX_BOLETO_FIELD)
    accounts = _prop_list(doc.props, knowledge.FINANCIAL_REGEX_BANK_FIELD)
    if not (money or boletos or accounts):
        return None
    amount, method = knowledge.FIN_AMOUNT_UNIDENTIFIED, knowledge.FIN_METHOD_ACCOUNT
    if money:
        amount, method = money[0], knowledge.FIN_METHOD_MONEY
    elif boletos:
        amount, method = knowledge.FIN_AMOUNT_BOLETO, knowledge.FIN_METHOD_BOLETO
    beneficiary = (
        _collapse(accounts[0]) or knowledge.FIN_BENEFICIARY_UNKNOWN
        if accounts
        else knowledge.FIN_BENEFICIARY_UNKNOWN
    )
    return {
        "amount": amount,
        "method": method,
        "beneficiary": beneficiary,
        "doc_id": doc.doc_id,
        "path": doc.path,
        "source_name": _source_label(doc)
        or knowledge.FIN_EVIDENCE_SOURCE_FALLBACK,
    }


def _extract_evidences(docs: list[_FinDoc]) -> list[dict[str, Any]]:
    """Evidências com dedup por assinatura e ordenação por valor desc (:166-201).

    Args:
        docs: documentos classificados do eixo.

    Returns:
        Lista de evidências com score/relevance/justification dos handlers
        (:885-896): 75 com valor identificado, 55 sem; "PIX" no method →
        ALTA, senão MÉDIA; ordenadas pelo valor parseado desc (não-numérico
        vai ao fim, sentinela -1.0 — mesma chave do ICARO :201).
    """
    seen: set[tuple[str | None, str | None]] = set()
    out: list[dict[str, Any]] = []
    for doc in docs:
        signature = (
            (_prop_list(doc.props, knowledge.FINANCIAL_REGEX_MONEY_FIELD) or [None])[0],
            (_prop_list(doc.props, knowledge.FINANCIAL_REGEX_BANK_FIELD) or [None])[0],
        )
        if signature in seen:
            continue
        entry = _evidence_entry(doc)
        if entry is None:
            continue
        seen.add(signature)
        out.append(_finding_from_evidence(entry))
    out.sort(key=lambda e: -_parse_amount(e["amount"]))
    return out


def _finding_from_evidence(entry: dict[str, Any]) -> dict[str, Any]:
    """Aplica o handler ``_finding_from_financial_evidence`` (:885-896).

    Args:
        entry: evidência crua de ``_evidence_entry``.

    Returns:
        Evidência com ``entity_name``, ``score`` (75/55), ``relevance``
        (PIX→ALTA), ``justification`` e ``source_name`` literais do ICARO.
    """
    has_amount = bool(entry["amount"]) and (
        entry["amount"] != knowledge.FIN_AMOUNT_UNIDENTIFIED
    )
    is_pix = knowledge.FIN_EVIDENCE_PIX_MARKER in entry["method"].upper()
    return {
        **entry,
        "entity_name": knowledge.FIN_EVIDENCE_ENTITY_TEMPLATE.format(
            method=entry["method"] or knowledge.FIN_EVIDENCE_SOURCE_FALLBACK,
            beneficiary=entry["beneficiary"] or knowledge.FIN_BENEFICIARY_UNKNOWN,
        ),
        "score": (
            knowledge.FIN_EVIDENCE_SCORE_WITH_AMOUNT
            if has_amount
            else knowledge.FIN_EVIDENCE_SCORE_NO_AMOUNT
        ),
        "relevance": (
            knowledge.RELEVANCE_ALTA
            if is_pix
            else knowledge.RELEVANCE_MEDIA
        ),
        "justification": knowledge.FIN_EVIDENCE_JUSTIFICATION_TEMPLATE.format(
            amount=entry["amount"], method=entry["method"]
        ),
    }


# --- cartões (financial_strategy.py:243-334) ----------------------------------


def _card_index(docs: list[_FinDoc]) -> dict[str, _FinDoc]:
    """Índice de docs por ``ufed:id`` e sufixo-UUID do nome (:244-252).

    Args:
        docs: documentos classificados do eixo.

    Returns:
        ``{chave: doc}`` para resolver ``ufed:jumpTargets`` de cartões (o
        jar publica o id da transação em ``ufed:id`` e, quando o id não
        consta, o sufixo após o último ``_`` do nome).
    """
    index: dict[str, _FinDoc] = {}
    for doc in docs:
        for value in _prop_list(doc.props, knowledge.CARD_ID_FIELD):
            index[value] = doc
        if knowledge.CARD_NAME_UUID_SEP in doc.name:
            index[doc.name.rsplit(knowledge.CARD_NAME_UUID_SEP, 1)[-1]] = doc
    return index


def _card_skeleton(doc: _FinDoc) -> dict[str, Any] | None:
    """Esqueleto do cartão a partir do doc-fonte (Credit :259-270 / Mobile :272-283).

    Args:
        doc: documento de categoria de cartão.

    Returns:
        ``(chave, cartão)`` implícitos no dict retornado (``"_key"``), ou
        ``None`` quando a categoria não identifica cartão (o gate aprovou
        a categoria-mãe mas nem toda ela é cartão em si).
    """
    props = doc.props
    if any(m in doc.category for m in knowledge.CARD_CREDIT_MARKERS):
        key = first_str(props, (knowledge.CARD_NUMBER_FIELD,))
        company = first_str(props, (knowledge.CARD_COMPANY_FIELD,))
        return {
            "_key": key or knowledge.CARD_UNKNOWN_KEY,
            "card_type": "Credit Card",
            "masked_number": key,
            "description": company,
            "company": company,
            "organization": "",
            "expiration": first_str(props, (knowledge.CARD_EXPIRATION_FIELD,)),
            "added_date": "",
            "source_wallet": first_str(props, (knowledge.CARD_WALLET_FIELD,)),
        }
    if any(m in doc.category for m in knowledge.CARD_MOBILE_MARKERS):
        key = first_str(props, (knowledge.CARD_DESCRIPTION_FIELD,))
        return {
            "_key": key or knowledge.CARD_UNKNOWN_KEY,
            "card_type": "Mobile Card",
            "masked_number": "",
            "description": key,
            "organization": first_str(props, (knowledge.CARD_ORGANIZATION_FIELD,)),
            "expiration": "",
            "added_date": first_str(props, (knowledge.CARD_PURCHASE_TIME_FIELD,)),
            "source_wallet": first_str(props, (knowledge.CARD_WALLET_FIELD,)),
        }
    return None


def _card_transaction(jump: _FinDoc) -> dict[str, Any]:
    """Transação a partir do doc-jump de transferência (:300-311)."""
    amounts = _prop_list(jump.props, knowledge.CARD_TX_AMOUNT_FIELD)
    return {
        "amount": _parse_amount(
            amounts[0] if amounts else knowledge.CARD_TX_AMOUNT_DEFAULT
        ),
        "currency": first_str(jump.props, (knowledge.CARD_TX_CURRENCY_FIELD,)),
        "date": first_str(jump.props, (knowledge.CARD_TX_DATE_FIELD,)),
        "status": first_str(jump.props, (knowledge.CARD_TX_STATUS_FIELD,)),
        "transfer_type": first_str(jump.props, (knowledge.CARD_TX_TYPE_FIELD,)),
        "payee": first_str(jump.props, (knowledge.CARD_TX_PAYEE_FIELD,)),
        "payer": first_str(jump.props, (knowledge.CARD_TX_PAYER_FIELD,)),
        "doc_id": jump.doc_id,
    }


def _card_location(jump: _FinDoc) -> dict[str, Any] | None:
    """Local de pagamento do doc-jump (:316-328): regex "lat,lon" no campo geo."""
    values = _prop_list(jump.props, knowledge.CARD_GEO_FIELD)
    if not values:
        return None
    match = re.search(knowledge.CARD_GEO_RE, values[0])
    if not match:
        return None
    return {
        "latitude": float(match.group(1)),
        "longitude": float(match.group(2)),
        "source_type": knowledge.CARD_GEO_TYPE,
        "first_timestamp": first_str(jump.props, (knowledge.CARD_TIMESTAMP_FIELD,)),
        "doc_id": jump.doc_id,
    }


def _resolve_card_jumps(
    card: dict[str, Any], jump_ids: list[str], index: dict[str, _FinDoc]
) -> None:
    """Anexa transações/locais do cartão pelos docs-jump resolvidos (:288-331).

    Args:
        card: cartão em construção (mutado: transactions/locations e
            ``total_spent`` somando apenas valores > 0, como o ICARO).
        jump_ids: ids de ``ufed:jumpTargets`` do doc-fonte.
        index: índice de docs do lote (jump fora do lote é ignorado em
            silêncio fiel — o ICARO só vê o que foi coletado).
    """
    for jump_id in jump_ids:
        jump = index.get(jump_id)
        if jump is None:
            continue
        if any(m in jump.category for m in knowledge.CARD_JUMP_TX_MARKERS):
            transaction = _card_transaction(jump)
            card["transactions"].append(transaction)
            if transaction["amount"] > 0:
                card["total_spent"] += transaction["amount"]
        elif knowledge.CARD_JUMP_LOCATION_MARKER in jump.category:
            location = _card_location(jump)
            if location is not None:
                card["locations"].append(location)


def _extract_cards(docs: list[_FinDoc]) -> list[dict[str, Any]]:
    """Cartões com transações/locais agregados por jumpTargets (:243-334).

    Args:
        docs: documentos classificados do eixo.

    Returns:
        Cartões deduplicados por número (crédito) ou descrição (mobile),
        cada um com ``transactions_count``, ``total_spent``, ``locations``,
        score ``min(60 + tx*2, 95)`` e relevância do handler (:900-911:
        score >= 85 ALTA senão MÉDIA — o handler não emite BAIXA aqui).
    """
    cards = _build_cards(docs)
    _attach_card_jumps(cards, _card_index(docs))
    return [_finding_from_card(card) for card in cards.values()]


def _build_cards(
    docs: list[_FinDoc],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Indexa os cartões por (chave, tipo) preservando os docs-fonte (:253-286).

    Args:
        docs: documentos classificados do eixo.

    Returns:
        ``{(chave, card_type): cartão}`` na ordem de descoberta; a chave é o
        número mascarado (crédito) ou a descrição (mobile), com
        ``CARD_UNKNOWN_KEY`` quando ausente — múltiplos docs do mesmo cartão
        caem na mesma entrada (``source_docs``).
    """
    cards: dict[tuple[str, str], dict[str, Any]] = {}
    for doc in docs:
        skeleton = _card_skeleton(doc)
        if skeleton is None:
            continue
        card_id = (skeleton.pop("_key"), skeleton["card_type"])
        card = cards.setdefault(
            card_id,
            {
                **skeleton,
                "doc_id": doc.doc_id,
                "path": doc.path,
                "source_docs": [],
                "transactions": [],
                "locations": [],
                "total_spent": 0.0,
            },
        )
        card["source_docs"].append(doc)
    return cards


def _attach_card_jumps(
    cards: dict[tuple[str, str], dict[str, Any]], index: dict[str, _FinDoc]
) -> None:
    """Passe 3 do ICARO (:288-331): resolve jumpTargets de cada doc-fonte.

    Args:
        cards: cartões de ``_build_cards`` (mutado).
        index: índice de docs do lote.
    """
    for card in cards.values():
        for doc in card["source_docs"]:
            _resolve_card_jumps(
                card, _prop_list(doc.props, knowledge.CARD_JUMPS_FIELD), index
            )


def _finding_from_card(card: dict[str, Any]) -> dict[str, Any]:
    """Aplica o handler ``_finding_from_card`` (:900-911) com amostra de tx.

    Args:
        card: cartão agregado de ``_extract_cards`` (mutado: sai sem as
            chaves internas de agregação; a lista completa de transações é
            substituída pela amostra das ``CARD_TRANSACTIONS_SAMPLE``
            últimas — o bulk fica fora do contexto do agente, §4.6).

    Returns:
        Cartão com ``entity_name``/``score``/``relevance``/``justification``
        literais do handler e marcadores ``transactions_truncated`` +
        ``transactions_omitted``.
    """
    count = len(card["transactions"])
    score = min(
        knowledge.CARD_SCORE_BASE + count * knowledge.CARD_SCORE_PER_TX,
        knowledge.CARD_SCORE_CAP,
    )
    sample = card["transactions"][-knowledge.CARD_TRANSACTIONS_SAMPLE:]
    label = card["description"] or card["masked_number"]
    public = {
        k: v for k, v in card.items() if k not in ("transactions", "source_docs")
    }
    return {
        **public,
        "transactions": sample,
        "transactions_count": count,
        "transactions_truncated": count > len(sample),
        "transactions_omitted": max(count - len(sample), 0),
        "entity_name": knowledge.CARD_ENTITY_TEMPLATE.format(
            card_type=card["card_type"], label=label
        ),
        "score": score,
        "relevance": (
            knowledge.RELEVANCE_ALTA
            if score >= knowledge.CARD_RELEVANCE_ALTA_MIN_SCORE
            else knowledge.RELEVANCE_MEDIA
        ),
        "justification": knowledge.CARD_JUSTIFICATION_TEMPLATE.format(
            count=count, total=card["total_spent"]
        ),
        "source_wallet": card["source_wallet"] or knowledge.CARD_WALLET_FALLBACK,
    }


# --- contas/ativos (financial_strategy.py:106-159) ----------------------------


def _account_uid(doc: _FinDoc) -> str:
    """Identificador da conta: primeiro ``ufed:id`` senão o nome (:137-138)."""
    ids = _prop_list(doc.props, knowledge.CARD_ID_FIELD)
    return ids[0] if ids else doc.name


def _extract_account_currencies(docs: list[_FinDoc]) -> dict[str, list[str]]:
    """Ativos financeiros agrupados pelo UUID da conta-pai no path (:122-134).

    Args:
        docs: documentos classificados do eixo.

    Returns:
        ``{uuid_conta: [moeda do ativo...]}`` — ativo = contentType
        ``financialasset`` OU categoria singular "financial account" (:126-128).
    """
    out: dict[str, list[str]] = {}
    for doc in docs:
        content_type = first_str(doc.props, (knowledge.CONTENT_TYPE_FIELD,)).lower()
        is_asset = (
            knowledge.ASSET_CONTENT_MARKER in content_type
            or (
                knowledge.FINANCIAL_ACCOUNT_MARKER in doc.category
                and knowledge.FINANCIAL_ACCOUNT_PLURAL_MARKER not in doc.category
            )
        )
        if not is_asset:
            continue
        match = re.search(knowledge.ACCOUNT_UUID_IN_PATH_RE, doc.raw_path)
        if not match:
            continue
        currencies = _prop_list(doc.props, knowledge.ASSET_CURRENCY_FIELD)
        out.setdefault(match.group(1), []).extend(currencies)
    return out


def _extract_accounts(docs: list[_FinDoc]) -> list[dict[str, Any]]:
    """Contas financeiras com agregados de ativos (:106-159 + §4.5).

    Args:
        docs: documentos classificados do eixo.

    Returns:
        Contas deduplicadas por uid (``ufed:id``/nome) com campos
        estruturados (:141-150), ``currencies``/``asset_count`` agregados
        (:153-157), conta de nuvem pelo e-mail ``.ufdr`` do path (:22/:139)
        e score/relevance do generic handler (90 → ALTA pelas bordas
        >=85/>=60, §4.5 — o ICARO não tem handler dedicado para a conta).
    """
    currencies = _extract_account_currencies(docs)
    accounts: dict[str, dict[str, Any]] = {}
    for doc in docs:
        content_type = first_str(doc.props, (knowledge.CONTENT_TYPE_FIELD,)).lower()
        if (
            knowledge.ACCOUNT_CONTENT_MARKER not in content_type
            and knowledge.FINANCIAL_ACCOUNT_PLURAL_MARKER not in doc.category
        ):
            continue
        uid = _account_uid(doc)
        if not uid or uid in accounts:
            continue
        accounts[uid] = _account_entry(doc, uid, currencies)
    return list(accounts.values())


def _account_entry(
    doc: _FinDoc, uid: str, currencies: dict[str, list[str]]
) -> dict[str, Any]:
    """Monta a conta de um doc-fonte com agregados e score fixo §4.5."""
    email = re.search(knowledge.EMAIL_IN_PATH_RE, doc.raw_path)
    account_currencies = currencies.get(uid, [])
    return {
        "ufed_id": uid,
        **{
            out_key: first_str(doc.props, (field_name,))
            for out_key, field_name in knowledge.ACCOUNT_OUTPUT_FIELDS
        },
        "source_account": email.group(1) if email else "",
        "currencies": sorted(set(account_currencies)),
        "asset_count": len(account_currencies),
        "doc_id": doc.doc_id,
        "path": doc.path,
        "score": knowledge.FIN_ACCOUNT_SCORE,
        "relevance": (
            knowledge.RELEVANCE_ALTA
            if knowledge.FIN_ACCOUNT_SCORE >= knowledge.RELEVANCE_ALTA_MIN_SCORE
            else knowledge.RELEVANCE_MEDIA
        ),
        "justification": knowledge.FIN_ACCOUNT_JUSTIFICATION,
    }


# --- envelope e tool (DESIGN 09-B §4.6/§4.7) ----------------------------------


def _truncated_payload(
    evidences: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
) -> dict[str, Any]:
    """Bloco ``truncated``: todo teto SEMPRE marcado, com omissões (§4.6)."""
    return {
        "total_cap_applied": scan.total_cap_applied,
        "query_caps_applied": list(scan.query_caps),
        "evidences_truncated": len(evidences) > knowledge.FINANCIAL_LIST_TOP,
        "evidences_omitted": max(
            len(evidences) - knowledge.FINANCIAL_LIST_TOP, 0
        ),
        "accounts_truncated": len(accounts) > knowledge.FINANCIAL_LIST_TOP,
        "accounts_omitted": max(len(accounts) - knowledge.FINANCIAL_LIST_TOP, 0),
        "http_budget_exceeded": budget.exceeded,
        "http_budget_calls_at_stop": budget.used if budget.exceeded else None,
        # docs_omitted carrega a QUANTIDADE num único elemento
        # (contrato fetch_within_budget: omitted[0]) — por isso sum(),
        # não len(): com 2+ docs omitidos len() subestimava a omissão
        # (R-TRIAGE-4; padrão já correto em media.py/devices_networks.py).
        "docs_omitted_by_budget": sum(docs_omitted),
    }


def _classified_docs(pairs: list[tuple[int, dict | None]]) -> list[_FinDoc]:
    """Aplica o gate :40-68 aos docs fetcheados (função única por par).

    Args:
        pairs: pares (doc_id, properties) do lote, props None = doc sumido.

    Returns:
        ``_FinDoc`` dos documentos que pertencem ao eixo, na ordem do lote.
    """
    docs: list[_FinDoc] = []
    for doc_id, props in pairs:
        if props is None:
            continue
        doc = _fin_doc(doc_id, props)
        if _passes_gate(doc):
            docs.append(doc)
    return docs


def _payload(
    sid: str,
    started: float,
    evidences: list[dict[str, Any]],
    cards: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    regex_availability: dict[str, int],
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
) -> dict[str, Any]:
    """Envelope §4.6 com o op_envelope de M-9 + contadores do eixo."""
    scan.warnings.extend(_budget_warnings(budget, docs_omitted))
    counters = dict(
        op_envelope(
            started,
            scanned=scan.docs_searched,
            processed=scan.classified,
        )
    )
    counters.update(
        docs_searched=scan.docs_searched,
        docs_fetched=scan.docs_fetched,
        docs_classified=scan.classified,
        http_calls=budget.used,
        cached=False,
    )
    if budget.exceeded:
        counters["phase"] = PHASE_PARTIAL
    truncated = _truncated_payload(
        evidences, accounts, scan, budget, docs_omitted
    )
    payload: dict[str, Any] = {
        "source_id": sid,
        "evidences": evidences[: knowledge.FINANCIAL_LIST_TOP],
        "evidence_count": len(evidences),
        "cards": cards,
        "accounts": accounts[: knowledge.FINANCIAL_LIST_TOP],
        "account_count": len(accounts),
        "regex_availability": regex_availability,
        "query_hits": scan.query_hits,
        "counters": counters,
        "truncated": truncated,
        "warnings": scan.warnings,
    }
    if not evidences and not cards and not accounts:
        payload["message"] = knowledge.FINANCIAL_EMPTY_MESSAGE
    return payload


def build_triage_financial(source_id: str) -> dict[str, Any]:
    """Executa a triagem financeira (motor T-4; ver ``triage_financial``).

    Args:
        source_id: source resolvido no Router (erros M-2 viram ValueError).

    Returns:
        Envelope do payload §4.6 da chamada corrente.
    """
    sid = (source_id or "").strip()
    if not sid:
        raise ValueError(_EMPTY_SID_MESSAGE)
    get_router().resolve(sid)
    started = time.monotonic()
    scan = _Scan()
    budget = _Budget(knowledge.TOOL_BUDGETS["triage_financial"])
    ids = _collect_candidate_ids(sid, scan, budget)
    regex_availability = _probe_regex_detectors(sid, scan, budget)
    scan.docs_searched = len(ids)
    omitted: list[int] = []
    pairs = _fetch_within_budget(sid, ids, budget, omitted)
    scan.docs_fetched = sum(1 for _, props in pairs if props is not None)
    docs = _classified_docs(pairs)
    scan.classified = len(docs)
    evidences = _extract_evidences(docs)
    cards = _extract_cards(docs)
    accounts = _extract_accounts(docs)
    return _payload(
        sid, started, evidences, cards, accounts,
        regex_availability, scan, budget, omitted,
    )


def triage_financial(source_id: str) -> dict[str, Any]:
    """Triagem financeira do eixo "Financeiro" do ICARO (T-4).

    Args:
        source_id: id do source no servidor WebAPI do caso.

    Returns:
        Envelope ``{source_id, evidences[:30] + evidence_count, cards,
        accounts[:30] + account_count, regex_availability, query_hits,
        counters, truncated, warnings[, message]}`` — evidências por
        detector Regex (escada MONEY > Boleto > Conta, valor BR parseado,
        ordenadas por valor desc, score 75/55, PIX→ALTA), cartões com
        transações/locais agregados por ``ufed:jumpTargets`` (score
        ``min(60+2×tx, 95)``, ≥85 ALTA; transações truncadas em 5 com
        omissão marcada) e contas/ativos financeiros (score fixo 90 com
        moedas/ativos agregados e conta de nuvem pelo e-mail do path).
        ``regex_availability`` informa o painel de sondas por caso (o
        detector ausente vira warning, nunca exceção — M-7).

    Raises:
        ToolError: source inexistente/servidor offline (M-2) ou erro
            inesperado traduzido; estouro de orçamento NUNCA raising — vira
            ``truncated.http_budget_exceeded`` + warning.
    """
    try:
        return build_triage_financial(source_id)
    except ValueError:
        raise
    except Exception as exc:  # traduz erro inesperado (padrão da família)
        raise ToolError(f"triage_financial falhou: {exc}") from exc


def register(mcp: FastMCP) -> None:
    """Registra a tool de triagem financeira (triage_financial) no servidor.

    Args:
        mcp: servidor FastMCP ativo.

    Returns:
        Nada.
    """
    mcp.add_tool(triage_financial)
