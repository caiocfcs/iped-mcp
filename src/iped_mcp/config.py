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
