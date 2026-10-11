"""Configurações de runtime do MCP (tunáveis por deployment via env)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

# load_dotenv() aqui (e não só em client.py) garante que IPED_MCP_WORKERS do
# .env esteja visível independentemente da ordem de import dos módulos.
load_dotenv()

# Paralelismo de fetch (workers) e, por consequência, o pool de conexões do
# requests (client.py casa pool_maxsize com este valor). 32 validado contra o
# IPED: escala linear 1→32 workers (345 docs/s), 0 erros com pool casado.
# Acima disso não ganha — o gargalo é N+1 × RTT, não banda/CPU do servidor.
WORKERS = max(1, int(os.environ.get("IPED_MCP_WORKERS", "32")))

# --- Resiliência (DESIGN 03 / M-3): retry ×3 + circuit breaker ----------------
# Números portados do iped-icaro, validados contra o IPED real em produção:
# retry ×3 com backoff 2^n (iped_client.py:28-29) e breaker de 4 falhas/60 s
# com sonda /sources de 3 s (iped_client.py:48-49,65-73).

# Tentativas por chamada (1 original + 2 re-tentativas).
RETRY_MAX_ATTEMPTS = max(1, int(os.environ.get("IPED_MCP_RETRY_ATTEMPTS", "3")))
# Base do backoff exponencial: espera = BASE ** tentativa → 1 s, 2 s, 4 s, ...
RETRY_BACKOFF_BASE = float(os.environ.get("IPED_MCP_RETRY_BACKOFF_BASE", "2"))
# Teto de uma única espera de backoff (env mal calibrado não trava o agente).
RETRY_BACKOFF_CAP = float(os.environ.get("IPED_MCP_RETRY_BACKOFF_CAP", "8"))
# Falhas de CONEXÃO consecutivas (retries já esgotados) para abrir o circuito.
CB_FAILURE_THRESHOLD = max(2, int(os.environ.get("IPED_MCP_CB_FAILURE_THRESHOLD", "4")))
# Janela fail-fast do circuito aberto; expirada, a sonda decide a reabertura.
CB_COOLDOWN_SECONDS = float(os.environ.get("IPED_MCP_CB_COOLDOWN_SECONDS", "60"))
# Timeout da sonda GET /sources/ que decide manter/reabrir o circuito.
CB_PROBE_TIMEOUT_SECONDS = float(os.environ.get("IPED_MCP_CB_PROBE_TIMEOUT_SECONDS", "3"))


def _positive_seconds(env_name: str, default: float) -> float:
    """Lê um timeout do ambiente, validando como segundos estritamente positivos.

    Args:
        env_name: Nome da variável de ambiente (ex.: ``IPED_MCP_TIMEOUT_SEARCH``).
        default: Valor em segundos usado quando a variável não está no ambiente.

    Returns:
        O valor efetivo em segundos (``> 0``).

    Raises:
        ValueError: Valor não numérico ou ``<= 0`` (timeout infinito/inválido
            travaria o agente sem diagnóstico).
    """
    raw = os.environ.get(env_name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{env_name} deve ser um nº de segundos: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{env_name} deve ser > 0: {raw!r}")
    return value


def _positive_int(env_name: str, default: int) -> int:
    """Le um limite inteiro do ambiente, validando como valor estritamente positivo.

    Args:
        env_name: Nome da variavel de ambiente (ex.: ``IPED_MCP_TEXT_CAP``).
        default: Valor inteiro usado quando a variavel nao esta no ambiente.

    Returns:
        O valor efetivo (``> 0``).

    Raises:
        ValueError: Valor nao inteiro ou ``<= 0`` (cap zero/negativo truncaria
            toda leitura a nada sem diagnostico).
    """
    raw = os.environ.get(env_name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{env_name} deve ser um n inteiro de bytes: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{env_name} deve ser > 0: {raw!r}")
    return value


# --- Timeouts por classe de operação (DESIGN 04 / M-4) -------------------------
# Substituem o timeout único de 10 s do baseline, que abortava cedo demais as
# operações longas reais (D-17/G8). Fonte única: lidos 1× aqui, override por env
# sem tocar código; o client.py escolhe a classe por método. Classes/defaults:
# Padrão das operações curtas (sources, bookmarks, mutações): 30 s — o default
# validado em produção pelo icaro (iped_client.py:27).
TIMEOUT_DEFAULT = _positive_seconds("IPED_MCP_TIMEOUT_DEFAULT", 30.0)
# Busca /search sobre o caso inteiro (sem paginação server-side): 30 s, mesmo
# default do icaro. O core sobe a 120 s localmente para match-all (AC4,
# preservado em core/tool.py) e o scan match-all mantém o seu (regex/scan.py).
TIMEOUT_SEARCH = _positive_seconds("IPED_MCP_TIMEOUT_SEARCH", 30.0)
# Propriedades/documento /docs/{id}: 90 s — o doc completo pode reabrir storage
# SQLite e descomprimir Media.tar no servidor; doc pesado leva > 30 s (razão
# literal em iped_client.py:38-40).
TIMEOUT_PROPS = _positive_seconds("IPED_MCP_TIMEOUT_PROPS", 90.0)
# Texto extraído /text (chunked, teto de 10 MB no client): 60 s, a ordem de
# grandeza do enrichment do icaro (transcript_enrichment_service.py:96-103).
TIMEOUT_TEXT = _positive_seconds("IPED_MCP_TIMEOUT_TEXT", 60.0)
# Download /content e /thumb — componente connect do par (connect, read): 10 s
# bastam para o handshake com servidor local; valor medido no baseline.
TIMEOUT_CONNECT_DOWNLOAD = _positive_seconds("IPED_MCP_TIMEOUT_CONNECT_DOWNLOAD", 10.0)
# Download — componente read: 120 s, arquivo grande exfiltrado em conexão
# lenta; era o teto do baseline (_CONTENT_READ_TIMEOUT), preservado.
TIMEOUT_READ_DOWNLOAD = _positive_seconds("IPED_MCP_TIMEOUT_READ_DOWNLOAD", 120.0)


# --- Teto de leitura do /text (DESIGN 08 / M-8) ---------------------------------
# O /text do jar-alvo e chunked e sem Content-Length: sem teto, um doc com texto
# extraido gigante estouraria a memoria do servidor MCP. O valor era o literal
# _TEXT_READ_CAP (10 MB) no client; vira constante com env ``IPED_MCP_TEXT_CAP``
# para o operador ajustar por caso sem tocar codigo. Default PRESERVADO (AC2):
# os 10 MB originais.
TEXT_CAP_BYTES = _positive_int("IPED_MCP_TEXT_CAP", 10 * 1024 * 1024)


# --- Diretório de exports (DESIGN 10 / M-10) ---------------------------------
# Fonte ÚNICA do destino padrão para todas as tools que gravam artefatos
# (core/bookmarks/regex/whatsapp). Antes era constante duplicada em
# regex/tool.py e whatsapp/tool.py e não valia para core/bookmarks (que
# exigiam dest_dir). A unificação completa da configuração de exports fica
# com o M-17 (Lote 3); aqui só a fonte única + alcance de todas as tools.
DEFAULT_EXPORTS_DIR = "/tmp/iped_mcp/exports"
EXPORTS_DIR = os.environ.get("IPED_MCP_EXPORTS_DIR", DEFAULT_EXPORTS_DIR)
