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
