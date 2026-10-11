"""Tool ``triage_devices_networks`` — eixo dispositivos/redes (T-7, DESIGN 09-B §7).

O eixo não re-ranqueia (score fixo 50 "Sem ranqueamento por relevância.
Coleta total.", devices_networks_strategy.py:44-59): separa 3 baldes —
logs (:70-72), wifi (:76-81) e bluetooth (:84-88) — e a inteligência está
toda no ``logs_networks_normalizer`` (parse ``Service=… IP=…`` de
``ufed:Body``, 3 formatos de ``ufed:Application``, agregação por IP com
colapso IPv6 por /64, resumo por serviço e fingerprints). O envelope MCP
expõe SOMENTE os agregados — a timeline bruta ``connections``[:1000]
(normalizer :35/:254-255) nunca sai daqui; o conceito portado é
``connections_total`` + ``connections_truncated`` (§7.4).

Consultas: ``category:"logs"`` + filtro de ``IDENTITY_CATEGORIES`` para
redes sem fio + filtro de ``IDENTITY_VALID_SOURCES`` para bluetooth
(reuso DRY ordenado pelo design §7.2 — nenhuma réplica literal das
tuplas de T-3). Eixo vazio: None no ICARO (build_payload :280-281); no
MCP, vazio explícito com ``message`` (§0.1). Erros M-2 e orçamento
R-TRIAGE-1 como no resto da família triage.
"""

from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from iped_mcp.fetch import search_ids
from iped_mcp.progress import PHASE_PARTIAL, op_envelope
from iped_mcp.router import get_router
from iped_mcp.triage import knowledge
from iped_mcp.triage.fields import first_str
from iped_mcp.triage.budget import (
    HttpBudget as _Budget,
    budget_warnings as _budget_warnings,
    fetch_within_budget as _fetch_within_budget,
)

_SEARCH_HINT = (
    "confira source_id (iped_list_sources); o eixo consulta category:\"logs\" "
    "e as categorias wireless/bluetooth derivadas de knowledge."
)
_EMPTY_SID_MESSAGE = "source_id é obrigatório e não pode ser vazio."

# Regex compiladas das constantes literais do normalizer (:40-64).
_RE_BODY = re.compile(knowledge.DN_LOG_BODY_RE)
_RE_APP_BRACKETS = re.compile(knowledge.DN_APP_BRACKETS_RE)
_RE_APP_DAEMON = re.compile(knowledge.DN_APP_DAEMON_RE)
_RE_APP_PAIR = re.compile(knowledge.DN_APP_PAIR_RE)
_RE_MODEL_ANY = re.compile(knowledge.DN_MODEL_ANY_RE)
_RE_TS = re.compile(knowledge.DN_TIMESTAMP_RE)


@dataclass
class _Scan:
    """Estado da varredura T-7 (queries, caps, contadores, avisos)."""

    query_hits: dict[str, int] = field(default_factory=dict)
    query_caps: list[str] = field(default_factory=list)
    total_cap_applied: bool = False
    docs_searched: int = 0
    docs_fetched: int = 0
    logs_total: int = 0
    warnings: list[str] = field(default_factory=list)


def _dn_queries() -> tuple[str, ...]:
    """Queries do eixo: logs + wireless + bluetooth (design §7.2).

    Returns:
        Tupla de queries ``category:"…"`` montadas a partir de
        ``DEVICES_NETWORKS_LOG_CATEGORY`` e dos filtros derivados de
        ``IDENTITY_CATEGORIES``/``IDENTITY_VALID_SOURCES`` (uma por
        categoria, ordem estável).
    """
    categories = (
        knowledge.DEVICES_NETWORKS_LOG_CATEGORY,
        *knowledge.DEVICES_NETWORKS_WIRELESS_CATEGORIES,
        *knowledge.DEVICES_NETWORKS_BLUETOOTH_CATEGORIES,
    )
    return tuple(
        knowledge.TELEPHONY_QUERY_TEMPLATE.format(category=category)
        for category in categories
    )


# --- parse de log (logs_networks_normalizer.py:96-182) --------------------------


def _norm_ip(raw: str) -> tuple[str, int, str | None] | None:
    """Valida IP e devolve ``(ip, versão, prefixo_/64_ou_None)`` (:96-106).

    Args:
        raw: texto bruto do campo ``IP=`` do corpo.

    Returns:
        Tupla normalizada; ``None`` quando o endereço é inválido (o
        normalizer descarta com log debug :166-168 — aqui via contagem
        ``logs_total`` vs ``logs_parsed``).
    """
    try:
        addr = ipaddress.ip_address(str(raw).strip())
    except ValueError:
        return None
    if addr.version == 6:
        return str(addr), 6, str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr), 4, None


def _parse_application(app: str) -> dict[str, str]:
    """``ufed:Application`` → {model, hw_model, build, os_version, app} (:108-144).

    Args:
        app: valor cru da propriedade.

    Returns:
        Dicionário com os 5 campos; ausentes = ``""``. Tenta formato B
        (colchetes), C (daemon) e A (par build;modelo) e cai no fallback
        ``DN_MODEL_ANY_RE`` — ordem e literais da fonte.
    """
    out = {"model": "", "hw_model": "", "build": "", "os_version": "", "app": ""}
    raw = str(app or "").strip()
    if not raw:
        return out
    for pattern, assign in (
        (_RE_APP_BRACKETS, _apply_brackets),
        (_RE_APP_DAEMON, _apply_daemon),
        (_RE_APP_PAIR, _apply_pair),
    ):
        # Formato B usa search (o bloche pode vir em qualquer ponto do
        # campo, :118); A/C usam match ancorado (:128/:135 — literais).
        matcher = pattern.search if pattern is _RE_APP_BRACKETS else pattern.match
        match = matcher(raw)
        if match:
            assign(out, match)
            return out
    fallback = _RE_MODEL_ANY.search(raw)
    if fallback:
        out["model"] = fallback.group(1)
    return out


def _apply_brackets(out: dict[str, str], match: re.Match) -> None:
    """Formato B ``<HW> <OS;ver;build> <bundle/…>`` (:118-126)."""
    out["hw_model"] = (match.group("hw_model") or "").strip()
    out["build"] = (match.group("build") or "").strip()
    out["os_version"] = (match.group("os_version") or "").strip()
    if match.group("bundle"):
        out["app"] = str(match.group("bundle"))


def _apply_daemon(out: dict[str, str], match: re.Match) -> None:
    """Formato C ``daemon/versao iPhoneX,Y/build`` (:128-133)."""
    out["model"] = match.group("model")
    out["build"] = match.group("build")
    out["app"] = f"{match.group('app')}/{match.group('app_version')}"


def _apply_pair(out: dict[str, str], match: re.Match) -> None:
    """Formato A ``build; iPhoneX,Y`` (:135-139)."""
    out["model"] = match.group("model")
    out["build"] = match.group("build")


def _parse_timestamp(ts_raw: str) -> str:
    """``14/08/2026 17:25:13 UTC`` → ISO ``2026-08-14T17:25:13`` (:146-156).

    Args:
        ts_raw: valor cru de ``ufed:TimeStamp``.

    Returns:
        ISO de segundos quando o padrão casa; o texto cru caso contrário
        (fiel à fonte — sem ``N/I`` inventado).
    """
    match = _RE_TS.match(str(ts_raw or ""))
    if not match:
        return str(ts_raw or "")
    try:
        stamp = time.strptime(f"{match.group('d')} {match.group('h')}",
                              "%d/%m/%Y %H:%M:%S")
        return time.strftime("%Y-%m-%dT%H:%M:%S", stamp)
    except ValueError:
        return str(ts_raw or "")


def _parse_log_entry(props: dict[str, Any]) -> dict[str, Any] | None:
    """Um LogEntry → registro normalizado; ``None`` fora do padrão (:158-182).

    Args:
        props: propriedades do doc.

    Returns:
        Registro interno ``{ts, service, ip, ip_version, app, model,
        hw_model, build, os_version}`` usado SÓ como insumo de agregação —
        jamais publicado no envelope (regra "nunca timeline bruta").
    """
    body = first_str(props, (knowledge.DN_LOG_BODY_FIELD,))
    match = _RE_BODY.search(body)
    if not match:
        return None
    ip_tuple = _norm_ip(match.group("ip_raw"))
    if not ip_tuple:
        return None
    ip, ip_version, _prefix = ip_tuple
    app = _parse_application(
        first_str(props, (knowledge.DN_APPLICATION_FIELD,))
    )
    return {
        "ts": _parse_timestamp(first_str(props, (knowledge.DN_TIMESTAMP_FIELD,))),
        "service": match.group("service").upper(),
        "ip": ip,
        "ip_version": ip_version,
        **app,
    }


# --- agregação (aggregate_logs :185-259, sem a timeline bruta) -------------------


def _span(slot: dict[str, Any], ts: str) -> None:
    """Atualiza ``first_seen``/``last_seen`` do agregado (:205-211)."""
    if not ts:
        return
    if not slot.get("first_seen") or ts < slot["first_seen"]:
        slot["first_seen"] = ts
    if ts > slot.get("last_seen", ""):
        slot["last_seen"] = ts


def _connections_by_ip(
    entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Conexões por IP com colapso IPv6 por /64, [:50] por hits (:196-218).

    Args:
        entries: registros parseados (ordem temporal preservada da fonte).

    Returns:
        ``(visão[:50], omissões)`` — cada entrada ``{ip, ip_version,
        first_seen, last_seen, hits, services[]}`` (``ip`` = prefixo /64
        para v6, fiel ao :213-215); ordenadas por hits desc.
    """
    by_ip: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = _aggregation_key(entry)
        if key is None:
            continue
        slot = by_ip.setdefault(key, {
            "ip": key, "ip_version": entry["ip_version"],
            "first_seen": entry["ts"], "last_seen": entry["ts"],
            "hits": 0, "services": set(),
        })
        slot["hits"] += 1
        slot["services"].add(entry["service"])
        _span(slot, entry["ts"])
    ordered = [
        {**slot, "services": sorted(slot["services"])}
        for slot in sorted(by_ip.values(), key=lambda s: -s["hits"])
    ]
    top = ordered[: knowledge.DN_CONNECTIONS_BY_IP_MAX]
    return top, len(ordered) - len(top)


def _aggregation_key(entry: dict[str, Any]) -> str | None:
    """Chave de agregação: IPv4 cru, IPv6 colapsado em /64 (:199-204).

    Args:
        entry: registro parseado.

    Returns:
        A chave; ``None`` quando o endereço não valida (defesa: parse já
        filtrou inválidos, o try porta o ``except`` do normalizer :203-204).
    """
    if entry["ip_version"] == 4:
        return entry["ip"]
    try:
        return str(ipaddress.ip_network(f"{entry['ip']}/64", strict=False))
    except ValueError:
        return None


def _services_summary(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resumo por serviço com rótulo legível e período (:220-230).

    Args:
        entries: registros parseados.

    Returns:
        Um item por serviço ``{service, label, hits, first_seen,
        last_seen}`` por hits desc; label = código cru quando fora de
        ``DN_SERVICE_LABELS`` (fallback :225).
    """
    by_service: dict[str, dict[str, Any]] = {}
    for entry in entries:
        slot = by_service.setdefault(entry["service"], {
            "service": entry["service"],
            "label": knowledge.DN_SERVICE_LABELS.get(
                entry["service"], entry["service"]
            ),
            "hits": 0, "first_seen": entry["ts"], "last_seen": entry["ts"],
        })
        slot["hits"] += 1
        _span(slot, entry["ts"])
    return sorted(by_service.values(), key=lambda s: -s["hits"])


def _device_fingerprints(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fingerprints dedup por (model, hw_model, build) (:232-251).

    Args:
        entries: registros parseados.

    Returns:
        Por hits desc; ``apps_observed`` sorted e acumulável entre
        formatos A/B/C; ``os_version`` = primeira não vazia (:244-245).
    """
    by_fp: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in entries:
        key = (entry["model"], entry["hw_model"], entry["build"])
        slot = by_fp.setdefault(key, {
            "model": entry["model"], "hw_model": entry["hw_model"],
            "build": entry["build"], "os_version": "",
            "apps_observed": set(),
            "first_seen": entry["ts"], "last_seen": entry["ts"], "hits": 0,
        })
        slot["hits"] += 1
        if entry["os_version"] and not slot["os_version"]:
            slot["os_version"] = entry["os_version"]
        if entry["app"]:
            slot["apps_observed"].add(entry["app"])
        _span(slot, entry["ts"])
    return [
        {**slot, "apps_observed": sorted(slot["apps_observed"])}
        for slot in sorted(by_fp.values(), key=lambda s: -s["hits"])
    ]


# --- baldes e coleta (devices_networks_strategy.py:63-89) ------------------------


def _bucket_documents(
    pairs: list[tuple[int, dict[str, Any] | None]], scan: _Scan
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Separa os docs nos 3 baldes da strategy (:70-89).

    Args:
        pairs: ``(doc_id, props|None)`` da varredura.
        scan: contadores (mutado; ``logs_total`` e ``docs_fetched``).

    Returns:
        ``(props_dos_logs, wifi_dedup, bluetooth_dedup)`` — ordem de
        descoberta preservada; dedup por valor stripado (:80/:87).
    """
    logs: list[dict[str, Any]] = []
    wifi: list[str] = []
    bluetooth: list[str] = []
    for _doc_id, props in pairs:
        if props is None:
            continue
        scan.docs_fetched += 1
        category = first_str(props, (knowledge.CATEGORY_FIELD,)).lower()
        if knowledge.DEVICES_NETWORKS_LOG_MARKER in category:
            logs.append(props)
            scan.logs_total += 1
            continue
        source = first_str(
            props, (knowledge.DN_UFED_SOURCE_FIELD,)
        ).lower()
        _bucket_wireless(wifi, category, source, props)
        _bucket_bluetooth(bluetooth, category, source, props)
    return logs, wifi, bluetooth


def _bucket_wireless(
    wifi: list[str], category: str, source: str, props: dict[str, Any]
) -> bool:
    """Balde wifi: marker na categoria OU ``wifi`` no ufed:Source (:76-81).

    Args:
        wifi: lista acumulada (mutada).
        category: categoria minúscula do doc.
        source: ``ufed:Source`` minúsculo.
        props: propriedades do doc.

    Returns:
        ``True`` quando o doc foi consumido pelo balde.
    """
    marker = knowledge.DEVICES_NETWORKS_WIRELESS_MARKER
    wifi_src = knowledge.DEVICES_NETWORKS_WIFI_SOURCE_MARKER
    if marker not in category and wifi_src not in source:
        return False
    ssid = first_str(props, knowledge.DN_SSID_FIELDS).strip()
    if ssid and ssid not in wifi:
        wifi.append(ssid)
    return True


def _bucket_bluetooth(
    bluetooth: list[str], category: str, source: str, props: dict[str, Any]
) -> None:
    """Balde bluetooth: marker na categoria OU no ufed:Source (:84-88).

    Args:
        bluetooth: lista acumulada (mutada).
        category: categoria minúscula do doc.
        source: ``ufed:Source`` minúsculo.
        props: propriedades do doc.

    Returns:
        Nada.
    """
    marker = knowledge.DEVICES_NETWORKS_BLUETOOTH_MARKER
    if marker not in category and marker not in source:
        return
    name = first_str(props, (knowledge.BLUETOOTH_NAME_FIELD,)).strip()
    if name and name not in bluetooth:
        bluetooth.append(name)


def _collect_candidates(sid: str, scan: _Scan, budget: _Budget) -> list[int]:
    """Executa as queries do eixo e deduplica candidatos (§0.2).

    Args:
        sid: source resolvido.
        scan: estado da varredura (mutado; 500 → hits -1 + warning, M-2).
        budget: orçamento de GET (mutado).

    Returns:
        Ids únicos na ordem de descoberta com caps por query e global
        aplicados e marcados (mesma política T-3/T-5/T-6).
    """
    out: list[int] = []
    seen: set[int] = set()
    for query in _dn_queries():
        if not budget.allows():
            budget.exceeded = True
            break
        try:
            ids = search_ids(query, sid, hint=_SEARCH_HINT)
        except ValueError as exc:
            scan.query_hits[query] = -1
            scan.warnings.append(f"query {query} falhou: {exc}")
            budget.record()
            continue
        budget.record()
        scan.query_hits[query] = len(ids)
        if len(ids) > knowledge.CATEGORY_QUERY_CAP:
            scan.query_caps.append(query)
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


# --- envelope e tool (DESIGN 09-B §7.6) ------------------------------------------


def _truncated_payload(
    wifi: list[str],
    bluetooth: list[str],
    ip_omitted: int,
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
) -> dict[str, Any]:
    """Bloco ``truncated``: teto de cada seção marcado com a omissão (§7.6).

    Args:
        wifi: lista completa de SSIDs (para medir o corte de [:100]).
        bluetooth: lista completa de nomes bluetooth.
        ip_omitted: IPs fora da visão [:50].
        scan: estado da varredura.
        budget: orçamento de GET.
        docs_omitted: docs não buscados por orçamento.

    Returns:
        Dicionário ``truncated`` (inclui o conceito portado
        ``connections_truncated`` da timeline do ICARO :254-255, agora sobre
        a visão agregada — o corpo bruto não existe no MCP).
    """
    wifi_omitted = max(len(wifi) - knowledge.DN_WIFI_NETWORKS_MAX, 0)
    bt_omitted = max(len(bluetooth) - knowledge.DN_BLUETOOTH_DEVICES_MAX, 0)
    return {
        "total_cap_applied": scan.total_cap_applied,
        "query_caps_applied": list(scan.query_caps),
        "connections_truncated": ip_omitted > 0,
        "connections_by_ip_omitted": ip_omitted,
        "wifi_truncated": wifi_omitted > 0,
        "wifi_omitted": wifi_omitted,
        "bluetooth_truncated": bt_omitted > 0,
        "bluetooth_omitted": bt_omitted,
        "http_budget_exceeded": budget.exceeded,
        "http_budget_calls_at_stop": budget.used if budget.exceeded else None,
        # docs_omitted carrega a QUANTIDADE num único elemento
        # (contrato _fetch_within_budget: docs_omitted[0],
        # identity.py:1204-1206) — por isso sum(), não len().
        "docs_omitted_by_budget": sum(docs_omitted),
    }


def build_triage_devices_networks(source_id: str) -> dict[str, Any]:
    """Executa a triagem de dispositivos/redes (motor T-7).

    Args:
        source_id: source resolvido no Router (erros M-2 viram ValueError).

    Returns:
        Envelope §7.6 da chamada corrente (agregados apenas; nunca a
        timeline bruta).
    """
    sid = (source_id or "").strip()
    if not sid:
        raise ValueError(_EMPTY_SID_MESSAGE)
    get_router().resolve(sid)
    started = time.monotonic()
    scan = _Scan()
    budget = _Budget(knowledge.TOOL_BUDGETS["triage_devices_networks"])
    ids = _collect_candidates(sid, scan, budget)
    scan.docs_searched = len(ids)
    omitted: list[int] = []
    pairs = _fetch_within_budget(sid, ids, budget, omitted)
    log_props, wifi, bluetooth = _bucket_documents(pairs, scan)
    entries = sorted(
        (e for e in map(_parse_log_entry, log_props) if e),
        key=lambda e: e.get("ts") or "",
    )
    return _payload(sid, started, entries, wifi, bluetooth, scan, budget, omitted)


def _payload(
    sid: str,
    started: float,
    entries: list[dict[str, Any]],
    wifi: list[str],
    bluetooth: list[str],
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
) -> dict[str, Any]:
    """Monta o envelope §7.6 a partir das agregações prontas."""
    scan.warnings.extend(_budget_warnings(budget, docs_omitted))
    by_ip, ip_omitted = _connections_by_ip(entries)
    counters = dict(
        op_envelope(started, scanned=scan.docs_searched, processed=len(entries))
    )
    counters.update(
        docs_searched=scan.docs_searched,
        docs_fetched=scan.docs_fetched,
        http_calls=budget.used,
        cached=False,
    )
    if budget.exceeded:
        counters["phase"] = PHASE_PARTIAL
    empty = not entries and not wifi and not bluetooth
    return {
        "source_id": sid,
        "connections_by_ip": by_ip,
        "services_summary": _services_summary(entries),
        "device_fingerprints": _device_fingerprints(entries),
        "wifi_networks": wifi[: knowledge.DN_WIFI_NETWORKS_MAX],
        "bluetooth_devices": bluetooth[: knowledge.DN_BLUETOOTH_DEVICES_MAX],
        "connections_total": len(entries),
        "logs_parsed": len(entries),
        "logs_total": scan.logs_total,
        "query_hits": scan.query_hits,
        "counters": counters,
        "truncated": _truncated_payload(
            wifi, bluetooth, ip_omitted, scan, budget, docs_omitted
        ),
        "warnings": scan.warnings,
        **({"message": knowledge.DN_EMPTY_MESSAGE} if empty else {}),
    }


def triage_devices_networks(source_id: str) -> dict[str, Any]:
    """Triagem de dispositivos/redes do ICARO (T-7): só agregados.

    Args:
        source_id: id do source no servidor WebAPI do caso.

    Returns:
        Envelope ``{source_id, connections_by_ip[:50] (IPv6 colapsado por
        /64, por hits), services_summary[] (rótulos iCloud),
        device_fingerprints[] (dedup model/hw/build, apps_observed),
        wifi_networks[:100], bluetooth_devices[:100], connections_total,
        logs_parsed/logs_total, query_hits, counters, truncated,
        warnings[, message]}`` — a timeline bruta de conexões do ICARO
        (normalizer :35) NUNCA é publicada; o conceito de truncamento é
        portado sobre a visão agregada. Parse fiel de ``ufed:Body``
        (``Service=… IP=…``) e dos 3 formatos de ``ufed:Application``.

    Raises:
        ToolError: source inexistente/servidor offline (M-2) ou erro
            inesperado traduzido; estouro de orçamento NUNCA raising — vira
            ``truncated.http_budget_exceeded`` + warning.
    """
    try:
        return build_triage_devices_networks(source_id)
    except ValueError:
        raise
    except Exception as exc:  # traduz erro inesperado (padrão da família)
        raise ToolError(f"triage_devices_networks falhou: {exc}") from exc


def register(mcp: FastMCP) -> None:
    """Registra a tool de triagem de dispositivos/redes no servidor.

    Args:
        mcp: servidor FastMCP ativo.

    Returns:
        Nada.
    """
    mcp.add_tool(triage_devices_networks)
