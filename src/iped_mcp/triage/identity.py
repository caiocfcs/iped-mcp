"""Tool ``triage_identity`` — eixo identidade (T-3, DESIGN 09-B §3).

Porta o núcleo determinístico do eixo "Identidade" do ICARO: varre as
categorias canônicas (``iped_mcp.triage.knowledge``), replica a
classificação de ``_extract_and_score``
(``iped-icaro/strategies/identity_strategy.py:148-207``), normaliza a
identidade principal (contas, contatos, SIM, Wi-Fi, Bluetooth,
device_info) e pontua o achado pelo handler
(``draft_report_iped_manager.py:845-862``). Dispositivos reconhecidos são
entidades próprias — nunca alimentam a identidade principal (:211-229).
Nenhum LLM, nenhum arquivo do caso: somente GET /search e GET /docs/{id}.

O orçamento de GET (§2.3/R-TRIAGE-1) vive como ``_Budget`` privado deste
módulo enquanto é o único consumidor (KISS); a extração para
``triage/budget.py`` compartilhado é o item R-TRIAGE-1 do backlog.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any

from fastmcp import FastMCP

from iped_mcp import fetch, progress
from iped_mcp.progress import op_envelope
from iped_mcp.router import get_router
from iped_mcp.triage import budget as _budget
from iped_mcp.triage import knowledge
from iped_mcp.triage.fields import first_str, first_str_key

_SEARCH_HINT = "triage_identity (busca de categoria de identidade)"
_EMPTY_SID_MESSAGE = "'source_id' é obrigatório (veja iped_list_sources)."
# Aviso de pré-sonda (§3.2): nunca o "0 silencioso" do piloto (R1).
_NAME_COVERAGE_WARNING = (
    "campos de nome testados sem cobertura ({fields}); {docs} docs "
    "varridos — identidade usará apenas os fallbacks de conta/e-mail"
)
# Retrofit R-TRIAGE-1: o mecanismo vive em triage/budget.py (DESIGN 09-B
# §2.3, módulo próprio por responsabilidade). Os aliases privados abaixo
# preservam o contrato de import usado pelas demais tools (identity foi o
# berço do contador na onda B-1); nenhum muda o comportamento.
_Budget = _budget.HttpBudget
_budget_warnings = _budget.budget_warnings
_fetch_within_budget = _budget.fetch_within_budget


@dataclass
class _IdentityAgg:
    """Acúmulo da identidade principal (espelho ``IdentityEntity``).

    Attributes:
        name: nome canônico (limpo de "(...)"), "" até inferido.
        confidence: ``ALTA``/``MÉDIA`` (BAIXA só via score no handler).
        emails: e-mails de conta (dedup case-insensitive no pós-processo).
        phone_numbers: telefones estruturados/SIM do dono.
        mentioned_phone_numbers: telefones do ``Regex:PHONE`` filtrado.
        aliases: aliases de conta/WhatsApp.
        usernames: usernames de conta.
        sim_msisdn: MSISDN do SIM (fonte siminfo/categoria sim).
        wifi_networks: SSIDs vistos (sem side-effect de alias, §3.3).
        bluetooth_devices: nomes bluetooth vistos.
        known_contacts: agenda do eixo ``{telefone E.164: nome}``.
        device_info: EntryName/EntryValue brutos por chave nativa.
        photo_id: ``ufed:Photo:id`` da conta principal.
        evidence_source_type: inferida no pós-processo (§3.3).
        first_doc_id: primeiro doc classificado (âncora do achado, §3.5).
        first_doc_path: path do doc âncora.
    """

    name: str = ""
    confidence: str = knowledge.DEFAULT_CONFIDENCE
    emails: list[str] = field(default_factory=list)
    phone_numbers: list[str] = field(default_factory=list)
    mentioned_phone_numbers: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    usernames: list[str] = field(default_factory=list)
    sim_msisdn: str = ""
    wifi_networks: list[str] = field(default_factory=list)
    bluetooth_devices: list[str] = field(default_factory=list)
    known_contacts: dict[str, str] = field(default_factory=dict)
    device_info: dict[str, list[str]] = field(default_factory=dict)
    photo_id: str = ""
    evidence_source_type: str = ""
    first_doc_id: int | None = None
    first_doc_path: str = ""


@dataclass
class _Scan:
    """Contadores e avisos da varredura de identidade (envelope §3.7)."""

    query_hits: dict[str, int] = field(default_factory=dict)
    query_caps: list[str] = field(default_factory=list)
    total_cap_applied: bool = False
    docs_searched: int = 0
    docs_fetched: int = 0
    classified_docs: int = 0
    recognized: list[dict[str, Any]] = field(default_factory=list)
    name_field_hits: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(knowledge.IDENTITY_NAME_FIELDS, 0)
    )
    name_candidates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _ufed_source(props: dict[str, Any]) -> str:
    """``ufed:Source`` minúsculo, tolerante a lista-ou-string (:156-162).

    Args:
        props: propriedades do doc.

    Returns:
        Fonte normalizada; ``""`` quando ausente.
    """
    raw = props.get(knowledge.UFED_SOURCE_FIELD) or []
    value = raw[0] if isinstance(raw, list) and raw else raw
    return str(value).lower() if value else ""


def _is_recognized(category: str, props: dict[str, Any]) -> bool:
    """Dupla guarda de reconhecido: categoria OU contentType (:166-170).

    Args:
        category: categoria já minúscula do doc.
        props: propriedades do doc.

    Returns:
        True quando o doc é um dispositivo reconhecido.
    """
    if knowledge.RECOGNIZED_CATEGORY in category:
        return True
    content_type = str(props.get(knowledge.CONTENT_TYPE_FIELD) or "").lower()
    return knowledge.RECOGNIZED_MARKER in content_type


def _valid_identity_doc(category: str, source_str: str) -> bool:
    """Gate de classificação: categoria válida OU fonte válida (:172-175).

    Args:
        category: categoria minúscula do doc.
        source_str: ``ufed:Source`` normalizado.

    Returns:
        True quando o doc entra na identidade principal.
    """
    if any(cat in category for cat in knowledge.IDENTITY_CATEGORIES):
        return True
    return source_str in knowledge.IDENTITY_VALID_SOURCES


def _count_name_field(scan: _Scan, props: dict[str, Any]) -> None:
    """Pré-sonda §3.2: histograma das variantes de nome em TODO doc varrido.

    Args:
        scan: contadores da varredura (mutado).
        props: propriedades do doc varrido.
    """
    _, field_key = first_str_key(props, knowledge.IDENTITY_NAME_FIELDS)
    if field_key:
        scan.name_field_hits[field_key] += 1


def _zip_entries(props: dict[str, Any]) -> list[tuple[str, str]]:
    """``ufed:EntryName`` x ``ufed:EntryValue`` pareados (:44-49/:102-107).

    Args:
        props: propriedades do doc.

    Returns:
        Pares ``(chave, valor)`` já stripados (zip_longest, fillvalue "").
    """
    names = props.get(knowledge.ENTRY_NAME_FIELD) or []
    values = props.get(knowledge.ENTRY_VALUE_FIELD) or []
    pairs: list[tuple[str, str]] = []
    for name, value in zip_longest(names, values, fillvalue=""):
        pairs.append((str(name).strip(), str(value).strip()))
    return pairs


def _fill_bucket(device_info: dict[str, list[str]], key: str, value: str) -> None:
    """Acrescenta ``value`` ao bucket nativo sem duplicar (Android :52/:53).

    Args:
        device_info: buckets por chave nativa (mutado).
        key: chave EntryName.
        value: valor EntryValue.
    """
    bucket = device_info.setdefault(key, [])
    if value not in bucket:
        bucket.append(value)


def _apply_device_entries(
    agg: _IdentityAgg, props: dict[str, Any], category: str, source_str: str
) -> None:
    """Device info Android (fonte nativa, skip "N/A") e Apple (categoria).

    Args:
        agg: identidade principal (mutada).
        props: propriedades do doc.
        category: categoria minúscula.
        source_str: ``ufed:Source`` normalizado.
    """
    if source_str in knowledge.ANDROID_ENTRY_SOURCES:
        _apply_android_entries(agg, props)
    if _is_device_info_category(props, category):
        for key, value in _zip_entries(props):
            if key and value:
                _fill_bucket(agg.device_info, key, value)


def _apply_android_entries(agg: _IdentityAgg, props: dict[str, Any]) -> None:
    """Entries nativos android com skip do "N/A" do jar (:49-53).

    Args:
        agg: identidade principal (mutada).
        props: propriedades do doc.
    """
    for key, value in _zip_entries(props):
        if key and value and value != knowledge.ENTRY_SKIP_VALUE:
            _fill_bucket(agg.device_info, key, value)


def _is_device_info_category(props: dict[str, Any], category: str) -> bool:
    """Categoria device info* COM os dois lists de entries presentes (:100-106).

    Args:
        props: propriedades do doc.
        category: categoria minúscula.

    Returns:
        True quando o branch Apple/geral deve rodar.
    """
    has_entries = props.get(knowledge.ENTRY_NAME_FIELD) and props.get(
        knowledge.ENTRY_VALUE_FIELD
    )
    if not has_entries:
        return False
    return any(
        marker in category for marker in knowledge.DEVICE_INFO_CATEGORY_MARKERS
    )


def _set_msisdn(agg: _IdentityAgg, value: str) -> None:
    """Registra MSISDN do SIM e o espelha em phone_numbers (:67-71/:76-79).

    Args:
        agg: identidade principal (mutada).
        value: número do SIM já limpo.
    """
    agg.sim_msisdn = value
    if value not in agg.phone_numbers:
        agg.phone_numbers.append(value)


def _apply_sim(
    agg: _IdentityAgg, props: dict[str, Any], category: str, source_str: str
) -> None:
    """SIM MSISDN: EntryName==MSISDN ou Category/Name canônicos (:57-79).

    Args:
        agg: identidade principal (mutada).
        props: propriedades do doc.
        category: categoria minúscula.
        source_str: ``ufed:Source`` normalizado.
    """
    if source_str != knowledge.SIM_SOURCE and knowledge.SIM_CATEGORY_MARKER not in category:
        return
    for key, value in _zip_entries(props):
        if (
            key == knowledge.MSISDN_ENTRY_KEY
            and value
            and value != knowledge.ENTRY_SKIP_VALUE
        ):
            _set_msisdn(agg, value)
    if not _sim_named_msisdn(props):
        return
    value = first_str(props, (knowledge.UFED_VALUE_FIELD,)).strip()
    if value and value != knowledge.ENTRY_SKIP_VALUE:
        _set_msisdn(agg, value)


def _sim_named_msisdn(props: dict[str, Any]) -> bool:
    """Categoria ``SIM/USIM MSISDN`` OU ``ufed:name`` com "msisdn" (:77-78).

    Args:
        props: propriedades do doc.

    Returns:
        True quando ``ufed:Value`` é um MSISDN rotulado.
    """
    if first_str(props, (knowledge.UFED_CATEGORY_FIELD,)) == knowledge.SIM_MSISDN_CATEGORY:
        return True
    return knowledge.MSISDN_NAME_MARKER in first_str(
        props, (knowledge.NAME_LOWER_FIELD,)
    ).lower()


def _apply_wifi_bluetooth(
    agg: _IdentityAgg, props: dict[str, Any], category: str, source_str: str
) -> None:
    """Wi-Fi (``ufed:SSId``) e Bluetooth (``ufed:DeviceName``) (:81-96).

    O side-effect de SSID virar alias NÃO é portado (decisão registrada no
    DESIGN 09-B §3.3, devices_networks_strategy.py:74-75).

    Args:
        agg: identidade principal (mutada).
        props: propriedades do doc.
        category: categoria minúscula.
        source_str: ``ufed:Source`` normalizado.
    """
    wifi = knowledge.WIFI_SOURCE_MARKER in source_str or knowledge.WIFI_CATEGORY_MARKER in category
    if wifi:
        ssid = first_str(props, (knowledge.SSID_FIELD,)).strip()
        if ssid and ssid not in agg.wifi_networks:
            agg.wifi_networks.append(ssid)
    bluetooth = knowledge.BLUETOOTH_MARKER in source_str or knowledge.BLUETOOTH_MARKER in category
    if bluetooth:
        device_name = first_str(props, (knowledge.BLUETOOTH_NAME_FIELD,)).strip()
        if device_name and device_name not in agg.bluetooth_devices:
            agg.bluetooth_devices.append(device_name)


def _account_email(props: dict[str, Any]) -> str:
    """E-mail da conta: ``ufed:EmailAddress``; ``ufed:Username`` só com "@"
    (identity_strategy.py:334-344).

    Args:
        props: propriedades do doc de conta.

    Returns:
        E-mail bruto; ``""`` quando não há.
    """
    email = first_str(props, (knowledge.EMAIL_FIELD,)).strip()
    if email:
        return email
    username = first_str(props, (knowledge.USERNAME_FIELD,)).strip()
    return username if knowledge.AT_MARKER in username else ""


def _append_unique(values: list[Any], target: list[str]) -> None:
    """Anexa valores não vazios sem duplicar (aliases/usernames :346-351).

    Args:
        values: candidatos (a lista vazia quando o campo não existe).
        target: lista de destino (mutada).
    """
    for value in values:
        if value and value not in target:
            target.append(value)


def _apply_whatsapp_account(agg: _IdentityAgg, props: dict[str, Any]) -> None:
    """Conta WhatsApp: phoneNumber→telefone, userName→alias/nome (:318-323).

    Args:
        agg: identidade principal (mutada).
        props: propriedades do doc de conta.
    """
    account_type = first_str(props, (knowledge.ACCOUNT_TYPE_FIELD,)).lower()
    if account_type != knowledge.WHATSAPP_ACCOUNT_MARKER:
        return
    wa_phone = first_str(props, (knowledge.WA_PHONE_FIELD,))
    if wa_phone and wa_phone not in agg.phone_numbers:
        agg.phone_numbers.append(wa_phone)
    wa_name = first_str(props, (knowledge.WA_NAME_FIELD,)).strip()
    _append_unique([wa_name], agg.aliases)
    if wa_name and not agg.name:
        agg.name = wa_name


def _apply_high_confidence_name(
    agg: _IdentityAgg, props: dict[str, Any], source_str: str
) -> None:
    """ALTA só com fonte google/health/... E ``ufed:Name`` presente (:328-332).

    Args:
        agg: identidade principal (mutada).
        props: propriedades do doc de conta.
        source_str: ``ufed:Source`` normalizado.
    """
    if not any(
        marker in source_str for marker in knowledge.HIGH_CONFIDENCE_SOURCE_MARKERS
    ):
        return
    classic_name = first_str(props, (knowledge.NAME_CLASSIC_FIELD,)).strip()
    if classic_name:
        agg.name = classic_name
        agg.confidence = knowledge.CONFIDENCE_ALTA


def _apply_account(agg: _IdentityAgg, props: dict[str, Any], source_str: str) -> None:
    """Conta de usuário: WhatsApp, confiança ALTA, e-mail, aliases (:302-351).

    Args:
        agg: identidade principal (mutada).
        props: propriedades do doc de conta.
        source_str: ``ufed:Source`` normalizado.
    """
    _apply_whatsapp_account(agg, props)
    _apply_high_confidence_name(agg, props, source_str)
    photo = first_str(props, (knowledge.PHOTO_ID_FIELD,))
    if photo and not agg.photo_id:
        agg.photo_id = photo
    email = _account_email(props)
    existing = [stored.lower() for stored in agg.emails]
    if email and email.lower() not in existing:
        agg.emails.append(email)
    _append_unique(list(props.get(knowledge.ACCOUNT_ALIAS_FIELD) or []), agg.aliases)
    _append_unique(list(props.get(knowledge.USERNAME_LIST_FIELD) or []), agg.usernames)


def _is_contacts_category(category: str) -> bool:
    """Ramo contatos: ``contatos`` exceto ``user accounts``, OU == contacts (:238).

    Args:
        category: categoria minúscula do doc.

    Returns:
        True quando o doc é de agenda (não de conta de usuário).
    """
    if knowledge.CONTACT_CATEGORY_MARKER not in category:
        return category == knowledge.CONTACT_CATEGORY_EXACT
    return knowledge.USER_ACCOUNTS_MARKER not in category


def _contact_phone_candidates(props: dict[str, Any]) -> list[str]:
    """Candidatos a telefone de contato: 4 campos concatenados (:366-393).

    Args:
        props: propriedades do doc de contato.

    Returns:
        Valores string na ordem de concatenação do ICARO.
    """
    candidates: list[str] = []
    for field_key in knowledge.KNOWN_CONTACT_PHONE_FIELDS:
        values = props.get(field_key) or []
        if isinstance(values, str):
            values = [values]
        candidates.extend(v for v in values if isinstance(v, str))
    return candidates


def _known_contact_phone(raw: str, name: str, owner_msisdn: str) -> str:
    """Valida um candidato da agenda e devolve a chave E.164 (:395-436).

    Args:
        raw: candidato bruto (pode trazer sufixo WhatsApp ``@...``).
        name: nome do contato (``DESCONHECIDO`` = sem nome legível).
        owner_msisdn: MSISDN do dono (recusa o próprio número).

    Returns:
        Chave normalizada ("+" só se o original tinha); ``""`` se recusa.
    """
    value = raw.strip()
    if not value or _contact_phone_rejected(value, name):
        return ""
    value = value.split(knowledge.AT_MARKER)[0]
    digits = re.sub(r"\D", "", value)
    if not (
        knowledge.KNOWN_CONTACT_MIN_DIGITS
        <= len(digits)
        <= knowledge.KNOWN_CONTACT_MAX_DIGITS
    ):
        return ""
    owner = re.sub(r"\D", "", owner_msisdn or "")
    if owner and digits == owner:
        return ""
    return "+" + digits if value.startswith("+") else digits


def _contact_phone_rejected(value: str, name: str) -> bool:
    """Rejeitos literais da agenda: grupo/newsletter e @lid sem nome (:403-411).

    Args:
        value: candidato bruto.
        name: nome do contato.

    Returns:
        True quando o candidato deve ser descartado.
    """
    if any(marker in value for marker in knowledge.KNOWN_CONTACT_REJECT_MARKERS):
        return True
    return knowledge.LID_MARKER in value and name == knowledge.DEFAULT_CONTACT_NAME


def _apply_contacts(agg: _IdentityAgg, props: dict[str, Any]) -> None:
    """Agenda do eixo: ``known_contacts[telefone] = nome`` (:353-436).

    Args:
        agg: identidade principal (mutada).
        props: propriedades do doc de contato.
    """
    name = first_str(props, knowledge.IDENTITY_NAME_FIELDS).strip()
    name = name or knowledge.DEFAULT_CONTACT_NAME
    for raw_phone in _contact_phone_candidates(props):
        clean = _known_contact_phone(raw_phone, name, agg.sim_msisdn)
        if clean and clean not in agg.known_contacts:
            agg.known_contacts[clean] = name


def _clean_phone_value(phone: Any) -> str:
    """Limpeza literal do ICARO: strip, aspas e \\n (:259/:266).

    Args:
        phone: valor bruto da propriedade de telefone.

    Returns:
        Valor limpo como string.
    """
    return str(phone).strip().strip('"').replace("\n", "")


def _account_phone_values(props: dict[str, Any]) -> list[Any]:
    """Telefones estruturados de conta: ``a or b`` do ICARO (:257).

    Args:
        props: propriedades do doc de conta.

    Returns:
        Valores da primeira lista não vazia na ordem dos campos.
    """
    for field_key in knowledge.ACCOUNT_PHONE_FIELDS:
        values = props.get(field_key) or []
        if values:
            return list(values)
    return []


def _is_decimal_phone(clean: str) -> bool:
    """Rejeita decimal: tem "." e o resto são só dígitos/sinais (:266-268).

    Args:
        clean: telefone bruto limpo.

    Returns:
        True quando o valor é um número decimal (não telefone).
    """
    if "." not in clean:
        return False
    digits = clean
    for char in knowledge.PHONE_DECIMAL_STRIP_CHARS:
        digits = digits.replace(char, "")
    return digits.isdigit()


def _passes_regex_phone_filters(clean: str) -> bool:
    """Os 5 filtros literais do Regex:PHONE + INT_MAX (:266-278).

    Args:
        clean: candidato limpo.

    Returns:
        True quando o valor pode ser um telefone citado.
    """
    digits = re.sub(r"\D", "", clean)
    if _is_decimal_phone(clean):
        return False
    if digits.startswith(knowledge.PHONE_ICCID_PREFIX) and len(digits) >= knowledge.PHONE_ICCID_MIN_DIGITS:
        return False
    indicators = any(char in clean for char in knowledge.PHONE_INDICATORS)
    if not indicators and len(digits) < knowledge.PHONE_MIN_BARE_DIGITS:
        return False
    if " " in clean and not re.search(knowledge.PHONE_SPACED_PATTERN_RE, clean):
        return False
    if not (
        knowledge.PHONE_MENTION_MIN_DIGITS
        <= len(digits)
        <= knowledge.PHONE_MENTION_MAX_DIGITS
    ):
        return False
    return knowledge.PHONE_INT_MAX_LITERAL not in digits


def _apply_phones(agg: _IdentityAgg, props: dict[str, Any], is_accounts: bool) -> None:
    """Telefones estruturados (só contas) e citados via Regex (:255-282).

    Args:
        agg: identidade principal (mutada).
        props: propriedades do doc.
        is_accounts: doc é do ramo de contas de usuário.
    """
    if is_accounts:
        for phone in _account_phone_values(props):
            clean = _clean_phone_value(phone)
            if clean and clean not in agg.phone_numbers:
                agg.phone_numbers.append(clean)
    for phone in props.get(knowledge.REGEX_PHONE_FIELD) or []:
        clean = _clean_phone_value(phone)
        if clean and _passes_regex_phone_filters(clean):
            if clean not in agg.mentioned_phone_numbers:
                agg.mentioned_phone_numbers.append(clean)


def _apply_doc(doc_id: int, props: dict[str, Any], agg: _IdentityAgg, scan: _Scan) -> None:
    """Classifica um doc e atualiza identidade/reconhecidos (:148-282).

    Args:
        doc_id: id do documento.
        props: propriedades do doc.
        agg: identidade principal (mutada).
        scan: contadores da varredura (mutado).
    """
    _count_name_field(scan, props)
    category = first_str(props, (knowledge.CATEGORY_FIELD,)).lower()
    source_str = _ufed_source(props)
    if _is_recognized(category, props):
        record = _recognized_record(
            props, first_str(props, (knowledge.PATH_FIELD,)), doc_id
        )
        if record:
            scan.recognized.append(record)
        return
    if not _valid_identity_doc(category, source_str):
        return
    if agg.first_doc_id is None:
        agg.first_doc_id = doc_id
        agg.first_doc_path = first_str(props, (knowledge.PATH_FIELD,))
    scan.classified_docs += 1
    candidate = first_str(props, knowledge.IDENTITY_NAME_FIELDS).strip()
    if candidate:
        scan.name_candidates.append(candidate)
    _apply_device_entries(agg, props, category, source_str)
    _apply_sim(agg, props, category, source_str)
    _apply_wifi_bluetooth(agg, props, category, source_str)
    is_accounts = any(marker in category for marker in knowledge.ACCOUNT_CATEGORY_MARKERS)
    is_contacts = _is_contacts_category(category)
    if is_accounts:
        _apply_account(agg, props, source_str)
    elif is_contacts:
        _apply_contacts(agg, props)
    _apply_phones(agg, props, is_accounts)


def _split_multi(value: Any) -> list[str]:
    """Multi-vírgula (lista OU string): IMEIs/IPs do reconhecido (:32-42).

    Args:
        value: valor bruto da propriedade.

    Returns:
        Partes stripadas não vazias.
    """
    if value is None:
        return []
    parts: list[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            parts.extend(str(item).split(","))
    else:
        parts = str(value).split(",")
    return [part.strip() for part in parts if part.strip()]


def _count_events(value: Any) -> int:
    """``ufed:jumpTargets`` (lista OU string "[a, b]") → nº de eventos (:45-58).

    Args:
        value: valor bruto da propriedade.

    Returns:
        Contagem de eventos; 0 quando ausente.
    """
    if value is None:
        return 0
    if isinstance(value, (list, tuple)):
        return len(value)
    text = str(value).strip()
    if not text:
        return 0
    text = text.removeprefix("[").removesuffix("]")
    return len([part for part in text.split(",") if part.strip()])


def _cloud_account_from_path(path: str) -> str:
    """Conta da nuvem no path (sufixo ``.ufdr`` removido) (:61-72).

    Args:
        path: caminho do doc no caso.

    Returns:
        E-mail da conta; ``""`` quando o path não traz nenhum.
    """
    if not path:
        return ""
    match = re.search(knowledge.CLOUD_ACCOUNT_UFDR_RE, path)
    if match:
        return match.group(1)
    match = re.search(knowledge.CLOUD_ACCOUNT_ANY_RE, path)
    return match.group(1).removesuffix(".ufdr") if match else ""


def _recognized_values(props: dict[str, Any], path: str) -> dict[str, Any]:
    """Extrai os atributos do reconhecido com a semântica por campo (:75-110).

    Args:
        props: propriedades do doc.
        path: caminho do doc (conta da nuvem).

    Returns:
        Atributos internos prontos (string/lista/contagem conforme o campo).
    """
    values: dict[str, Any] = {}
    for attr, field_key in knowledge.RECOGNIZED_DEVICE_FIELDS.items():
        raw = props.get(field_key)
        if attr in knowledge.RECOGNIZED_MULTI_FIELDS:
            values[attr] = _split_multi(raw)
        elif attr == knowledge.RECOGNIZED_COUNT_FIELD:
            values[attr] = _count_events(raw)
        else:
            values[attr] = first_str(props, (field_key,))
    values[knowledge.RECOGNIZED_ACCOUNT_FIELD] = _cloud_account_from_path(path)
    return values


def _recognized_row(values: dict[str, Any], attr: str) -> Any:
    """Valor de saída de um atributo no shape format_for_prompt (:133-149).

    Args:
        values: atributos extraídos.
        attr: atributo interno.

    Returns:
        Valor com o default do campo ([]/0/""/"N/I"; marca cai em fabricante).
    """
    value = values.get(attr)
    if attr in knowledge.RECOGNIZED_MULTI_FIELDS:
        return list(value or [])
    if attr == knowledge.RECOGNIZED_COUNT_FIELD:
        return int(value or 0)
    if attr == knowledge.RECOGNIZED_BRAND_FIELD:
        return value or values.get(knowledge.RECOGNIZED_MANUFACTURER_FIELD) or knowledge.DEVICE_INFO_UNKNOWN
    default = knowledge.RECOGNIZED_OUTPUT_EMPTY_DEFAULTS.get(
        attr, knowledge.DEVICE_INFO_UNKNOWN
    )
    return value or default


def _recognized_record(
    props: dict[str, Any], path: str, doc_id: int
) -> dict[str, Any] | None:
    """Um doc → registro compacto de reconhecido; None sem essencial (:82-86).

    Args:
        props: propriedades do doc.
        path: caminho do doc.
        doc_id: id do documento (âncora p/ iped_doc_info).

    Returns:
        Dict com os 14 rótulos PT do ICARO + ``doc_id``/``path``; None quando
        não há model, imei nem serial (não é RecognizedDevice).
    """
    values = _recognized_values(props, path)
    if not any(values[attr] for attr in knowledge.RECOGNIZED_ESSENTIAL_FIELDS):
        return None
    row = {
        label: _recognized_row(values, attr)
        for attr, label in knowledge.RECOGNIZED_OUTPUT_LABELS.items()
    }
    row["doc_id"] = doc_id
    row["path"] = path
    return row


def _post_process(agg: _IdentityAgg) -> None:
    """Limpeza e inferências finais (:435-500, ordem do ICARO preservada).

    Args:
        agg: identidade principal (mutada).
    """
    _fallback_phone_from_device_info(agg)
    _infer_name_from_email(agg)
    agg.name = re.sub(knowledge.PARENTHETICAL_CLEAN_RE, "", agg.name).strip()
    agg.emails = _clean_emails(agg.emails)
    agg.phone_numbers = _normalize_phones(agg.phone_numbers)
    agg.evidence_source_type = _evidence_source(agg)


def _fallback_phone_from_device_info(agg: _IdentityAgg) -> None:
    """Sem telefone estruturado, usa MSISDN do device_info (:439-443).

    Args:
        agg: identidade principal (mutada).
    """
    if agg.phone_numbers:
        return
    for key in knowledge.MSISDN_FALLBACK_KEYS:
        bucket = agg.device_info.get(key)
        if bucket:
            agg.phone_numbers.append(bucket[0])
            return


def _infer_name_from_email(agg: _IdentityAgg) -> None:
    """Sem nome, inferred de e-mail de domínio pessoal (:445-450).

    Args:
        agg: identidade principal (mutada).
    """
    if agg.name:
        return
    for email in agg.emails:
        if any(domain in email.lower() for domain in knowledge.PERSONAL_EMAIL_DOMAINS):
            agg.name = email
            return


def _clean_emails(emails: list[str]) -> list[str]:
    """Remove "(\u2026)" e deduplica case-insensitive mantendo 1a forma (:459-464).

    Args:
        emails: e-mails brutos do eixo.

    Returns:
        Lista limpa e única.
    """
    cleaned = [
        re.sub(knowledge.PARENTHETICAL_CLEAN_RE, "", email).strip()
        for email in emails
    ]
    seen: set[str] = set()
    unique: list[str] = []
    for email in cleaned:
        if email.lower() not in seen:
            seen.add(email.lower())
            unique.append(email)
    return unique


def _norm_phone_e164(raw: str) -> str:
    """E.164 tolerant: "+" só se o original tinha; mínimo 8 dígitos (:467-471).

    Args:
        raw: telefone bruto.

    Returns:
        Número normalizado; ``""`` quando não presta.
    """
    clean = re.sub(knowledge.PARENTHETICAL_CLEAN_RE, "", raw).strip()
    digits = re.sub(r"\D", "", clean)
    if len(digits) < knowledge.PHONE_MIN_NORM_DIGITS:
        return ""
    return "+" + digits if clean.startswith("+") else digits


def _normalize_phones(phones: list[str]) -> list[str]:
    """Normaliza e deduplica por contenção, mantendo o primeiro visto (:473-487).

    Args:
        phones: telefones brutos do eixo.

    Returns:
        Telefones normalizados únicos.
    """
    normalized: list[str] = []
    seen_bare: set[str] = set()
    for raw in phones:
        norm = _norm_phone_e164(raw)
        if not norm:
            continue
        bare = norm.lstrip("+")
        if any(bare in seen or seen in bare for seen in seen_bare):
            continue
        seen_bare.add(bare)
        normalized.append(norm)
    return normalized


def _evidence_source(agg: _IdentityAgg) -> str:
    """Inferência da origem da evidência (:489-500).

    Args:
        agg: identidade principal.

    Returns:
        Um dos literais ``EVIDENCE_*`` do knowledge.
    """
    if agg.device_info or agg.sim_msisdn or agg.bluetooth_devices:
        return knowledge.EVIDENCE_DEVICE
    if not agg.emails:
        return knowledge.EVIDENCE_UNKNOWN
    if _email_matches(agg.emails, knowledge.ICLOUD_EMAIL_MARKERS):
        return knowledge.EVIDENCE_ICLOUD
    if _email_matches(agg.emails, knowledge.GOOGLE_EMAIL_MARKERS):
        return knowledge.EVIDENCE_GOOGLE
    return knowledge.EVIDENCE_ONLINE


def _email_matches(emails: list[str], markers: tuple[str, ...]) -> bool:
    """Algum e-mail contém algum marcador (case-insensitive; :493-496).

    Args:
        emails: e-mails da identidade.
        markers: marcadores de domínio (gmail/icloud/...).

    Returns:
        True quando algum par (e-mail, marcador) casa.
    """
    return any(
        marker in email.lower() for email in emails for marker in markers
    )


def _apply_device_alias(result: dict[str, str], native_key: str, value: str) -> None:
    """Primeira alias que preenche vence (schema :504-527).

    Args:
        result: schema padronizado (mutado).
        native_key: chave nativa do device_info.
        value: primeiro valor da bucket nativa.
    """
    for std_key, aliases in knowledge.DEVICE_INFO_SCHEMA.items():
        if native_key in aliases and result[std_key] == knowledge.DEVICE_INFO_UNKNOWN:
            result[std_key] = value
            return


def _fill_account_key(
    result: dict[str, str], key: str, emails: list[str], markers: tuple[str, ...]
) -> None:
    """Preenche google/icloud account a partir dos e-mails (:533-543).

    Args:
        result: schema padronizado (mutado).
        key: chave do schema a preencher.
        emails: e-mails da identidade.
        markers: marcadores de domínio que qualificam a conta.
    """
    if result[key] != knowledge.DEVICE_INFO_UNKNOWN:
        return
    for email in emails:
        if any(marker in email.lower() for marker in markers):
            result[key] = email
            return


def _standardize_device_info(agg: _IdentityAgg) -> dict[str, str]:
    """Schema comum de 16 chaves + regras de preenchimento (§3.4).

    Args:
        agg: identidade principal.

    Returns:
        ``device_info`` padronizado (ausente = ``DEVICE_INFO_UNKNOWN``).
    """
    result = dict.fromkeys(
        knowledge.DEVICE_INFO_SCHEMA, knowledge.DEVICE_INFO_UNKNOWN
    )
    for native_key, values in agg.device_info.items():
        if values:
            _apply_device_alias(result, native_key, str(values[0]).strip())
    if (
        result[knowledge.DEVICE_KEY_MSISDN] == knowledge.DEVICE_INFO_UNKNOWN
        and agg.sim_msisdn
    ):
        result[knowledge.DEVICE_KEY_MSISDN] = agg.sim_msisdn
    _fill_account_key(
        result,
        knowledge.DEVICE_KEY_GOOGLE_ACCOUNT,
        agg.emails,
        knowledge.GOOGLE_EMAIL_MARKERS,
    )
    _fill_account_key(
        result,
        knowledge.DEVICE_KEY_ICLOUD_ACCOUNT,
        agg.emails,
        knowledge.ICLOUD_ACCOUNT_EMAIL_MARKERS,
    )
    return result


def _identity_relevance(score: int) -> str:
    """Relevância por valor: >=85 ALTA, >=60 MÉDIA, senão BAIXA (:856).

    Args:
        score: score do achado.

    Returns:
        Um dos literais ``RELEVANCE_*`` do knowledge.
    """
    if score >= knowledge.IDENTITY_RELEVANCE_ALTA_MIN_SCORE:
        return knowledge.RELEVANCE_ALTA
    if score >= knowledge.IDENTITY_RELEVANCE_MEDIA_MIN_SCORE:
        return knowledge.RELEVANCE_MEDIA
    return knowledge.RELEVANCE_BAIXA


def _identity_justification(agg: _IdentityAgg) -> str:
    """Justificação literal do handler com amostras de 2 (:848-852).

    Args:
        agg: identidade principal pós-processada.

    Returns:
        Texto "Entidade de identidade (confiança: X) | E-mails: ... | Tel: ...".
    """
    text = knowledge.IDENTITY_JUSTIFICATION_TEMPLATE.format(confidence=agg.confidence)
    if agg.emails:
        text += knowledge.IDENTITY_JUSTIFICATION_EMAILS_SUFFIX.format(
            emails=", ".join(agg.emails[: knowledge.JUSTIFICATION_SAMPLE_SIZE])
        )
    if agg.phone_numbers:
        text += knowledge.IDENTITY_JUSTIFICATION_PHONES_SUFFIX.format(
            phones=", ".join(agg.phone_numbers[: knowledge.JUSTIFICATION_SAMPLE_SIZE])
        )
    return text


def _known_contacts_sample(known: dict[str, str]) -> list[dict[str, str]]:
    """Amostra determinística da agenda (ordem de descoberta, teto §3.7).

    Args:
        known: agenda completa ``{telefone: nome}``.

    Returns:
        Até ``KNOWN_CONTACTS_SAMPLE`` entradas ``{phone, name}``.
    """
    return [
        {"phone": phone, "name": name}
        for phone, name in list(known.items())[: knowledge.KNOWN_CONTACTS_SAMPLE]
    ]


def _identity_payload(agg: _IdentityAgg) -> dict[str, Any]:
    """Bloco ``identity`` do envelope (§3.7 + campos do achado §3.5).

    Args:
        agg: identidade principal pós-processada.

    Returns:
        Projeção canônica (espelho _serialize_identity draft:549-581) com
        score/relevance/justificação do handler e a âncora doc_id/path.
    """
    score = knowledge.IDENTITY_SCORE_BY_CONFIDENCE.get(
        agg.confidence, knowledge.IDENTITY_SCORE_DEFAULT
    )
    return {
        "name": agg.name or knowledge.NOT_IDENTIFIED_NAME,
        "confidence": agg.confidence,
        "score": score,
        "relevance": _identity_relevance(score),
        "justification": _identity_justification(agg),
        "emails": list(agg.emails),
        "phone_numbers": list(agg.phone_numbers),
        "mentioned_phone_numbers": list(agg.mentioned_phone_numbers),
        "aliases": list(agg.aliases),
        "usernames": list(agg.usernames),
        "sim_msisdn": agg.sim_msisdn,
        "wifi_networks": list(agg.wifi_networks),
        "bluetooth_devices": list(agg.bluetooth_devices),
        "known_contacts_count": len(agg.known_contacts),
        "known_contacts_sample": _known_contacts_sample(agg.known_contacts),
        "device_info": _standardize_device_info(agg),
        "evidence_source_type": agg.evidence_source_type,
        "photo_id": agg.photo_id,
        "source_name": agg.evidence_source_type or knowledge.EVIDENCE_DEVICE,
        "doc_id": agg.first_doc_id,
        "path": agg.first_doc_path,
    }


def _collect_candidate_ids(sid: str, scan: _Scan, budget: _Budget) -> list[int]:
    """Busca as 19 categorias canônicas com dedup global, tetos e orçamento.

    Args:
        sid: source já resolvido (M-2).
        scan: contadores da varredura (mutado; query 500 vira hits -1 +
            warning e o eixo continua, §0.1).
        budget: orçamento de GET (mutado; cada /search conta 1).

    Returns:
        ids únicos na ordem de descoberta, até ``TOTAL_CANDIDATE_CAP``.
    """
    seen: dict[int, None] = {}
    for category in knowledge.IDENTITY_CATEGORIES:
        if not budget.allows():
            budget.exceeded = True
            break
        query = knowledge.IDENTITY_QUERY_TEMPLATE.format(category=category)
        try:
            ids = fetch.search_ids(query, sid, hint=_SEARCH_HINT)
        except ValueError as exc:
            budget.record()
            scan.warnings.append(f"query {query} falhou: {exc}")
            scan.query_hits[query] = -1
            continue
        budget.record()
        scan.query_hits[query] = len(ids)
        if len(ids) > knowledge.CATEGORY_QUERY_CAP:
            scan.query_caps.append(query)
            ids = ids[: knowledge.CATEGORY_QUERY_CAP]
        for doc_id in ids:
            if len(seen) >= knowledge.TOTAL_CANDIDATE_CAP:
                scan.total_cap_applied = True
                break
            seen[doc_id] = None
    return list(seen)


def _name_coverage_warnings(scan: _Scan) -> list[str]:
    """Pré-sonda §3.2: cobertura zero de nome com docs varridos > 0.

    Args:
        scan: contadores da varredura.

    Returns:
        Aviso nomeando os literais testados (filosofia M-7, nunca o
        0-silencioso do piloto); vazio quando houve cobertura ou nada foi
        varrido.
    """
    if scan.docs_fetched and not any(scan.name_field_hits.values()):
        return [
            _NAME_COVERAGE_WARNING.format(
                fields=", ".join(knowledge.IDENTITY_NAME_FIELDS),
                docs=scan.docs_fetched,
            )
        ]
    return []


def _payload(
    sid: str,
    started: float,
    agg: _IdentityAgg,
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
) -> dict[str, Any]:
    """Envelope final §3.7: projeção + contadores M-9 + http_calls + tetos.

    Args:
        sid: source triado.
        started: ``time.monotonic()`` do início.
        agg: identidade principal (pode estar vazia).
        scan: contadores da varredura.
        budget: orçamento da chamada.
        docs_omitted: docs sem props por orçamento (0-elementos = sem corte).

    Returns:
        Envelope com ``identity`` somente quando o eixo produziu identidade
        (espelho _serialize_identity draft:549-558) e ``message`` quando o
        eixo inteiro está vazio.
    """
    scan.warnings.extend(_name_coverage_warnings(scan))
    scan.warnings.extend(_budget_warnings(budget, docs_omitted))
    counters = dict(
        op_envelope(
            started,
            scanned=scan.docs_searched,
            processed=scan.classified_docs + len(scan.recognized),
        ),
        docs_searched=scan.docs_searched,
        docs_fetched=scan.docs_fetched,
        classified_docs=scan.classified_docs,
        recognized_total=len(scan.recognized),
        name_field_hits=dict(scan.name_field_hits),
        http_calls=budget.used,
        cached=False,
    )
    if budget.exceeded:
        counters["phase"] = progress.PHASE_PARTIAL
    envelope: dict[str, Any] = {
        "source_id": sid,
        "recognized_devices": scan.recognized[: knowledge.RECOGNIZED_DEVICES_MAX],
        "recognized_devices_count": len(scan.recognized),
        "query_hits": dict(scan.query_hits),
        "counters": counters,
        "truncated": _truncated_payload(agg, scan, budget),
        "warnings": list(scan.warnings),
    }
    if agg.first_doc_id is not None:
        envelope["identity"] = _identity_payload(agg)
    elif not scan.recognized:
        envelope["message"] = knowledge.IDENTITY_EMPTY_MESSAGE
    return envelope


def _truncated_payload(
    agg: _IdentityAgg, scan: _Scan, budget: _Budget
) -> dict[str, Any]:
    """Bloco ``truncated``: todos os tetos SEMPRE marcados, com omitidos (§3.7).

    Args:
        agg: identidade principal (para o tamanho da agenda).
        scan: contadores da varredura.
        budget: orçamento da chamada.

    Returns:
        Flags/contagens de corte de catálogo, reconhecidos, agenda e
        orçamento HTTP (``http_budget_calls_at_stop`` só quando estourou).
    """
    recognized_count = len(scan.recognized)
    known_count = len(agg.known_contacts)
    return {
        "total_cap_applied": scan.total_cap_applied,
        "query_caps_applied": list(scan.query_caps),
        "recognized_truncated": recognized_count > knowledge.RECOGNIZED_DEVICES_MAX,
        "recog_omitted": max(
            recognized_count - knowledge.RECOGNIZED_DEVICES_MAX, 0
        ),
        "known_contacts_truncated": known_count > knowledge.KNOWN_CONTACTS_SAMPLE,
        "known_contacts_omitted": max(
            known_count - knowledge.KNOWN_CONTACTS_SAMPLE, 0
        ),
        "http_budget_exceeded": budget.exceeded,
        "http_budget_calls_at_stop": budget.used if budget.exceeded else None,
    }


def build_triage_identity(source_id: str) -> dict[str, Any]:
    """Núcleo da triagem de identidade (lógica testável sem MCP).

    Args:
        source_id: source a triar (obrigatório).

    Returns:
        Envelope de ``_payload`` (shape DESIGN 09-B §3.7).

    Raises:
        ValueError: source_id vazio, inexistente ou endpoint offline
            (textos estáveis da política M-2, via ``Router.resolve``).
    """
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError(_EMPTY_SID_MESSAGE)
    get_router().resolve(sid)  # M-2 antes de qualquer HTTP (§0.1)
    started = time.monotonic()
    scan = _Scan()
    budget = _Budget(knowledge.TOOL_BUDGETS["triage_identity"])
    ids = _collect_candidate_ids(sid, scan, budget)
    omitted: list[int] = []
    pairs = _fetch_within_budget(sid, ids, budget, omitted)
    scan.docs_searched = len(ids)
    scan.docs_fetched = sum(1 for _, props in pairs if props is not None)
    agg = _IdentityAgg()
    for doc_id, props in pairs:
        if props is not None:
            _apply_doc(doc_id, props, agg, scan)
    if agg.first_doc_id is not None:
        _apply_name_fallback(agg, scan)
        _post_process(agg)
    return _payload(sid, started, agg, scan, budget, omitted)


def _apply_name_fallback(agg: _IdentityAgg, scan: _Scan) -> None:
    """Fallback de nome: primeiro candidato dos docs classificados (:285-292).

    O ICARO relê ``ufed:Name`` de qualquer ranked item ao fim da varredura;
    aqui a whitelist T-1.1 é reutilizada (mesma decisão do veredito R1 que
    abriu o eixo telefonia — cobertura sem 0-silencioso).

    Args:
        agg: identidade principal (mutada só se estiver sem nome).
        scan: contadores da varredura (candidatos coletados por doc).
    """
    if agg.name or not scan.name_candidates:
        return
    agg.name = scan.name_candidates[0]


def triage_identity(source_id: str) -> dict[str, Any]:
    """Triagem do eixo identidade: entidade principal + reconhecidos + agenda.

    Varre as 19 categorias canônicas de identidade, replica a classificação
    do eixo ICARO (categoria OU fonte válida; reconhecidos por dupla guarda
    categoria/contentType) e devolve a identidade principal normalizada
    (nome, confiança, e-mails, telefones com os 5 filtros do Regex:PHONE,
    SIM/Wi-Fi/Bluetooth, agenda, device_info padronizado em 16 chaves) com
    score 90/70/50 por confiança e relevância >=85/>=60 — heurística do
    fluxo ICARO, sem LLM. Dispositivos reconhecidos pela conta de nuvem são
    entidades próprias (não alimentam a identidade) com teto 50 sempre
    marcado. A resposta nunca traz propriedades brutas nem listas de ids.

    Cada chamada emite no máximo ``TOOL_BUDGETS["triage_identity"]`` GETs
    internos; o envelope reporta ``http_calls`` (tráfego real) e estouro vira
    ``truncated.http_budget_exceeded`` + warning, nunca exceção. A pré-sonda
    de campos de nome (``counters.name_field_hits``) elimina o 0-silencioso:
    cobertura zero com docs varridos emite aviso nomeando os literais.

    Args:
        source_id: source a triar (veja iped_list_sources).

    Returns:
        {source_id, identity? (nome, confidence, score, relevance,
        justification, emails, phone_numbers, mentioned_phone_numbers,
        aliases, usernames, sim_msisdn, wifi_networks, bluetooth_devices,
        known_contacts_count, known_contacts_sample[:20], device_info{16},
        evidence_source_type, photo_id, source_name, doc_id, path),
        recognized_devices ([:50], rótulos PT), recognized_devices_count,
        query_hits (-1 = query que falhou), counters (op_envelope +
        docs_searched/docs_fetched/classified_docs/recognized_total/
        name_field_hits/http_calls/cached), truncated (tetos SEMPRE
        marcados), warnings}; ``message`` quando o eixo está vazio (nunca
        exceção).

    Raises:
        ValueError: source_id vazio, inexistente ou servidor indisponível
            (textos estáveis da política M-2).
    """
    return build_triage_identity(source_id)


def register(mcp: FastMCP) -> None:
    """Registra a tool de triagem de identidade (T-3) no servidor.

    Args:
        mcp: servidor FastMCP ativo.

    Returns:
        Nada.
    """
    mcp.add_tool(triage_identity)
