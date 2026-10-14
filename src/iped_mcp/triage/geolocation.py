"""Tool ``triage_geolocation`` — eixo geolocalização (T-5, DESIGN 09-B §5).

Porta o motor de ``geolocation_strategy.py``: pré-sonda ORDENADA de probes
(§5.1 — o ICARO aceitava QUALQUER item com props geo :58-62/:215-221; a
coleta vinha do YAML de targets, ausente, então a sonda é MCP e cada 0-hit
ou falha vira warning nominal, M-7), gate por propriedades fiel
(:215-221 — QUALQUER chave com "latitude"/"longitude"/"geo:location"; a
query é só acesso ao candidato), extração do primeiro ponto válido por doc
(:157-212), filtros de bounds e Null Island (:82-88 + NULL_ISLAND_EPS),
agrupamento por 4 decimais (~11 m, :90-94) e pontuação do ACHADO pelo
handler ``_finding_from_geolocation`` (draft_report:932-947).

SEM enriquecimento de país: ``CoordinatesToCountryTool`` é ICARO-side/
externa (rede/ML — fora do contrato WebAPI do iped-mcp, §5.3). Campos
country/state/city simplesmente NÃO aparecem no shape: ausente ≠ falso.

Cobertura honesta (§5.1): docs geo em categorias não sondadas só entram se
a probe de campo pegar; a limitação é declarada em cada warning de sonda.
Nenhum LLM, nenhum arquivo do caso: só ``GET /search``/``GET /docs/{id}``
com orçamento R-TRIAGE-1.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any


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
    "confira source_id (iped_list_sources); o eixo depende das probes de "
    "knowledge.GEO_PROBE_QUERIES — categorias reais a confirmar em runtime "
    "(TO_VERIFY-B2)."
)
_EMPTY_SID_MESSAGE = "source_id é obrigatório e não pode ser vazio."


@dataclass
class _Scan:
    """Estado da varredura T-5 (probes, caps, contadores, avisos)."""

    query_hits: dict[str, int] = field(default_factory=dict)
    query_caps: dict[str, int] = field(default_factory=dict)
    total_cap_applied: bool = False
    docs_searched: int = 0
    docs_fetched: int = 0
    geo_docs: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Cluster:
    """Cluster de coordenadas (~11 m) em construção."""

    latitude: float
    longitude: float
    evidence_count: int = 0
    unique_coordinates: set[tuple[float, float]] = field(default_factory=set)
    source_types: set[str] = field(default_factory=set)
    timestamps: list[str] = field(default_factory=list)
    coordinate_fields: set[str] = field(default_factory=set)


def _has_geolocation(props: dict[str, Any]) -> bool:
    """Gate fiel a ``geolocation_strategy.py:215-221`` (a VERDADE do eixo).

    Args:
        props: propriedades do doc no envelope do jar.

    Returns:
        ``True`` quando QUALQUER chave contém "latitude", "longitude" ou
        "geo:location" (case-insensitive) — a query é só o acesso.
    """
    return any(
        marker in key.lower()
        for key in props
        for marker in knowledge.GEO_GATE_KEY_MARKERS
    )


def _in_bounds(lat: float, lon: float) -> bool:
    """Bounds [-90,90]/[-180,180] e descarte de Null Island (:82-88).

    Args:
        lat: latitude em graus.
        lon: longitude em graus.

    Returns:
        ``False`` fora do mapa OU |lat|<=NULL_ISLAND_EPS E
        |lon|<=NULL_ISLAND_EPS (0,0 é placeholder de falha de leitura —
        0.0002 NÃO é descartada, pino de teste §5.6).
    """
    if not knowledge.GEO_LAT_MIN <= lat <= knowledge.GEO_LAT_MAX:
        return False
    if not knowledge.GEO_LON_MIN <= lon <= knowledge.GEO_LON_MAX:
        return False
    return not (
        abs(lat) <= knowledge.NULL_ISLAND_EPS
        and abs(lon) <= knowledge.NULL_ISLAND_EPS
    )


def _source_type(category: str, from_geo_field: bool) -> str:
    """Tipo de fonte do ponto: cadeia de categorias fiel (:165-174) + §5.2.

    Args:
        category: categoria do doc (minúscula).
        from_geo_field: o ponto veio do campo ``common:geo:locations``?

    Returns:
        ``EXIF``/``CHAT``/``WIFI``/``CELL_TOWER`` por substring da categoria,
        ``GEO_METADATA`` para o campo geo do jar, senão a categoria crua
        (``GEO_TYPE_NO_CATEGORY`` quando vazia — design §5.2).
    """
    for markers, label in knowledge.GEO_SOURCE_TYPE_BY_CATEGORY:
        if any(marker in category for marker in markers):
            return label
    if from_geo_field:
        return knowledge.GEO_TYPE_GEO_METADATA
    return category or knowledge.GEO_TYPE_NO_CATEGORY


def _dynamic_point(props: dict[str, Any]) -> tuple[float, float, tuple[str, ...]] | None:
    """Ponto das chaves dinâmicas *latitude*/*longitude* (:176-193).

    Args:
        props: propriedades do doc.

    Returns:
        ``(lat, lon, chaves)`` quando existe par parseável DENTRO dos
        bounds e não-Null Island; ``None`` caso contrário (a estratégia
        original retorna o primeiro par; par inválido não impede o fallback
        geo:locations — porta-se pela mesma ordem de tentativa).
    """
    lat_str = lon_str = ""
    lat_field = lon_field = ""
    for key, values in props.items():
        lowered = key.lower()
        if "latitude" in lowered and values:
            lat_str, lat_field = str(values[0]), key
        elif "longitude" in lowered and values:
            lon_str, lon_field = str(values[0]), key
    if not (lat_str and lon_str):
        return None
    try:
        lat, lon = float(lat_str), float(lon_str)
    except ValueError:
        return None
    if not _in_bounds(lat, lon):
        return None
    # lat_str/lon_str só existem quando o campo correspondente existe: o
    # par de campos é sempre completo aqui.
    return lat, lon, (lat_field, lon_field)


def _geo_field_point(
    props: dict[str, Any],
) -> tuple[float, float, str] | None:
    """Ponto do fallback ``common:geo:locations`` por regex (:195-212).

    Args:
        props: propriedades do doc.

    Returns:
        ``(lat, lon, campo)`` do primeiro valor que casa
        ``GEO_COORDINATE_RE`` dentro dos bounds e não-Null Island; ``None``
        quando nada casa.
    """
    pattern = re.compile(knowledge.GEO_COORDINATE_RE)
    values = props.get(knowledge.GEO_LOCATION_FIELD) or []
    if isinstance(values, str):
        values = [values]
    for value in values:
        match = pattern.search(str(value))
        if not match:
            continue
        lat, lon = float(match.group(1)), float(match.group(2))
        if _in_bounds(lat, lon):
            return lat, lon, knowledge.GEO_LOCATION_FIELD
    return None


def _doc_point(
    props: dict[str, Any], category: str
) -> dict[str, Any] | None:
    """Primeiro ponto geográfico válido do doc (:157-212 + timestamp :223-229).

    Args:
        props: propriedades do doc.
        category: categoria minúscula do doc (mapeia o tipo de fonte).

    Returns:
        ``{latitude, longitude, source_type, coordinate_fields, timestamp}``
        (um ponto por doc — a strategy original conta UMA evidência por
        item, :116-117) ou ``None`` quando nada válido.
    """
    dynamic = _dynamic_point(props)
    if dynamic is not None:
        lat, lon, fields_used = dynamic
        from_geo = False
    else:
        geo = _geo_field_point(props)
        if geo is None:
            return None
        lat, lon, field_used = geo
        fields_used = (field_used,)
        from_geo = True
    return {
        "latitude": lat,
        "longitude": lon,
        "source_type": _source_type(category, from_geo),
        "coordinate_fields": sorted(fields_used),
        "timestamp": first_str(props, knowledge.GEO_TIMESTAMP_FIELDS),
    }


def _cluster_points(points: list[dict[str, Any]]) -> list[_Cluster]:
    """Agrupa por ``(round(lat,4), round(lon,4))`` ≈ 11 m (:90-94).

    Args:
        points: pontos extraídos (um por doc).

    Returns:
        Clusters na ordem de primeira aparição, cada um com
        ``evidence_count`` (docs que contribuíram, :115/:126),
        ``unique_coordinates`` (cruas únicas no cluster — diz se o IPED
        poluiu duplicando, :127-133), ``source_types`` (:165-174) e
        timestamps para o período.
    """
    clusters: dict[tuple[float, float], _Cluster] = {}
    for point in points:
        key = (
            round(point["latitude"], knowledge.GEO_CLUSTER_DECIMALS),
            round(point["longitude"], knowledge.GEO_CLUSTER_DECIMALS),
        )
        cluster = clusters.setdefault(
            key, _Cluster(latitude=key[0], longitude=key[1])
        )
        cluster.evidence_count += 1
        cluster.unique_coordinates.add((point["latitude"], point["longitude"]))
        cluster.source_types.add(point["source_type"])
        cluster.coordinate_fields.update(point["coordinate_fields"])
        cluster.timestamps.append(point["timestamp"])
    return list(clusters.values())


def _finding_from_cluster(cluster: _Cluster) -> dict[str, Any]:
    """Aplica o handler ``_finding_from_geolocation`` (draft_report:932-947).

    Args:
        cluster: cluster agregado de ``_cluster_points``.

    Returns:
        Ponto com ``score`` ``min(50 + count×5, 95)`` e ``relevance`` pelos
        limiares ESTRITOS de contagem (:934: >10 ALTA, >3 MÉDIA, senão
        BAIXA — count 3 é BAIXA) + período lexicográfico (:135-149: vazios
        ordenam antes e viram ``GEO_NOT_IDENTIFIED_TIMESTAMP``).
    """
    count = cluster.evidence_count
    ordered = sorted(cluster.timestamps)
    return {
        "latitude": cluster.latitude,
        "longitude": cluster.longitude,
        "source_types": sorted(cluster.source_types),
        "coordinate_fields": sorted(cluster.coordinate_fields),
        "evidence_count": count,
        "unique_coordinate_count": len(cluster.unique_coordinates),
        "first_seen": ordered[0] or knowledge.GEO_NOT_IDENTIFIED_TIMESTAMP,
        "last_seen": ordered[-1] or knowledge.GEO_NOT_IDENTIFIED_TIMESTAMP,
        "score": min(
            knowledge.GEO_SCORE_BASE + count * knowledge.GEO_SCORE_PER_EVIDENCE,
            knowledge.GEO_SCORE_CAP,
        ),
        "relevance": _geolocation_relevance(count),
        "justification": knowledge.GEO_JUSTIFICATION_TEMPLATE.format(
            lat=cluster.latitude,
            lon=cluster.longitude,
            count=count,
            first=ordered[0] or knowledge.GEO_NOT_IDENTIFIED_TIMESTAMP,
            last=ordered[-1] or knowledge.GEO_NOT_IDENTIFIED_TIMESTAMP,
        ),
    }


def _geolocation_relevance(count: int) -> str:
    """Relevância por contagem ESTREITA (:934; count exato 3 é BAIXA)."""
    if count > knowledge.GEO_RELEVANCE_ALTA_COUNT:
        return knowledge.RELEVANCE_ALTA
    if count > knowledge.GEO_RELEVANCE_MEDIA_COUNT:
        return knowledge.RELEVANCE_MEDIA
    return knowledge.RELEVANCE_BAIXA


def _probe_candidates(
    sid: str, scan: _Scan, budget: _Budget
) -> list[int]:
    """Executa as probes §5.1 e deduplica candidatos (cap por probe/query).

    Args:
        sid: source_id resolvido.
        scan: estado da varredura (mutado; cada 0-hit/falha vira warning
            nominal com a query testada — M-7, nunca silêncio).
        budget: orçamento de GET (mutado; cada /search conta 1).

    Returns:
        Ids únicos na ordem de descoberta; ``total_cap_applied`` marca o
        corte do teto global.
    """
    out: list[int] = []
    seen: set[int] = set()
    for query in knowledge.GEO_PROBE_QUERIES:
        if not budget.allows():
            budget.exceeded = True
            break
        try:
            ids = search_ids(query, sid, hint=_SEARCH_HINT)
        except ValueError as exc:
            scan.query_hits[query] = -1
            scan.warnings.append(f"probe {query} falhou: {exc}")
            budget.record()
            continue
        budget.record()
        scan.query_hits[query] = len(ids)
        if not ids:
            scan.warnings.append(
                knowledge.GEO_PROBE_ZERO_WARNING.format(query=query)
            )
            continue
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


# --- envelope e tool (DESIGN 09-B §5.4/§5.5) ----------------------------------


def _coordinates_by_source(
    clusters: list[_Cluster],
) -> dict[str, int]:
    """``{tipo → evidências}``: soma as evidências dos clusters de cada tipo."""
    out: dict[str, int] = {}
    for cluster in clusters:
        for source_type in cluster.source_types:
            out[source_type] = out.get(source_type, 0) + cluster.evidence_count
    return out


def _truncated_payload(
    findings: list[dict[str, Any]],
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
) -> dict[str, Any]:
    """Bloco ``truncated``: tetos SEMPRE marcados com omissões (§5.4)."""
    return {
        "total_cap_applied": scan.total_cap_applied,
        "query_caps_applied": list(scan.query_caps),
        "points_truncated": len(findings) > knowledge.GEO_POINTS_TOP,
        "points_omitted": max(
            len(findings) - knowledge.GEO_POINTS_TOP, 0
        ),
        "http_budget_exceeded": budget.exceeded,
        "http_budget_calls_at_stop": budget.used if budget.exceeded else None,
        # docs_omitted carrega a QUANTIDADE num único elemento
        # (contrato fetch_within_budget: omitted[0]) — por isso sum(),
        # não len(): com 2+ docs omitidos len() subestimava a omissão
        # (R-TRIAGE-4; padrão já correto em media.py/devices_networks.py).
        "docs_omitted_by_budget": sum(docs_omitted),
    }


def _payload(
    sid: str,
    started: float,
    findings: list[dict[str, Any]],
    by_source: dict[str, int],
    scan: _Scan,
    budget: _Budget,
    docs_omitted: list[int],
) -> dict[str, Any]:
    """Envelope §5.4 com op_envelope de M-9 + contadores do eixo."""
    scan.warnings.extend(_budget_warnings(budget, docs_omitted))
    counters = dict(
        op_envelope(
            started, scanned=scan.docs_searched, processed=scan.geo_docs
        )
    )
    counters.update(
        docs_searched=scan.docs_searched,
        docs_fetched=scan.docs_fetched,
        geo_docs=scan.geo_docs,
        http_calls=budget.used,
        cached=False,
    )
    if budget.exceeded:
        counters["phase"] = PHASE_PARTIAL
    return {
        "source_id": sid,
        "points": findings[: knowledge.GEO_POINTS_TOP],
        "locations_count": len(findings),
        "coordinates_by_source": by_source,
        "query_hits": scan.query_hits,
        "counters": counters,
        "truncated": _truncated_payload(findings, scan, budget, docs_omitted),
        "warnings": scan.warnings,
        **(
            {"message": knowledge.GEO_EMPTY_MESSAGE} if not findings else {}
        ),
    }


def build_triage_geolocation(source_id: str) -> dict[str, Any]:
    """Executa a triagem de geolocalização (motor T-5; ver ``triage_geolocation``).

    Args:
        source_id: source resolvido no Router (erros M-2 viram ValueError).

    Returns:
        Envelope do payload §5.4 da chamada corrente.
    """
    sid = (source_id or "").strip()
    if not sid:
        raise ValueError(_EMPTY_SID_MESSAGE)
    get_router().resolve(sid)
    started = time.monotonic()
    scan = _Scan()
    budget = _Budget(knowledge.TOOL_BUDGETS["triage_geolocation"])
    ids = _probe_candidates(sid, scan, budget)
    scan.docs_searched = len(ids)
    omitted: list[int] = []
    pairs = _fetch_within_budget(sid, ids, budget, omitted)
    scan.docs_fetched = sum(1 for _, props in pairs if props is not None)
    points = []
    for doc_id, props in pairs:
        if props is None or not _has_geolocation(props):
            continue
        category = first_str(props, (knowledge.CATEGORY_FIELD,)).lower()
        point = _doc_point(props, category)
        if point is not None:
            points.append(point)
    scan.geo_docs = len(points)
    clusters = _cluster_points(points)
    clusters.sort(key=lambda c: -c.evidence_count)
    findings = [_finding_from_cluster(cluster) for cluster in clusters]
    return _payload(
        sid,
        started,
        findings,
        _coordinates_by_source(clusters),
        scan,
        budget,
        omitted,
    )
