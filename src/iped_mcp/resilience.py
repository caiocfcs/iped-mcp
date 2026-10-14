"""Resiliência a falhas transitórias: política de retry e circuit breaker (M-3).

Padrão portado do iped-icaro (`iped_client.py:76-131` retry; `:48-49,:82-124`
circuit breaker), reescrito sobre a taxonomia de erros do M-2
(`iped_mcp.errors`) para que a decisão de retry seja por CLASSE de erro:

- `RetryPolicy`: até `config.RETRY_MAX_ATTEMPTS` tentativas com backoff
  exponencial 2^n (com teto); re-tenta SOMENTE falha de conexão, timeout e
  HTTP 5xx. 4xx NUNCA é re-tentado (contrato da taxonomia M-2). Mutações
  (POST/PUT/DELETE) só re-tentam falha de conexão pré-resposta: timeout ou
  5xx numa mutação são resposta ambígua (a escrita pode ter ocorrido) e o
  design exige cautela com bookmarks.
- `CircuitBreaker`: um por base_url (o `IPEDClient` é um por endpoint).
  Após `config.CB_FAILURE_THRESHOLD` chamadas consecutivas com falha de
  conexão (retries esgotados), abre e falha rápido pela janela de
  `config.CB_COOLDOWN_SECONDS`; expirada a janela, uma sonda `GET /sources/`
  de timeout curto decide a reabertura. A mesma sonda, no momento de abrir,
  distingue "servidor morto" de "operação lenta" (doc pesado > timeout não
  derruba a triagem inteira — icaro :109-124).

Teto de tempo total por chamada (AC5): `tentativas × timeout + backoffs`
(ex.: GET padrão 10 s: 3×10 s + 1 s + 2 s = 33 s; endpoints de stream com
read de 120 s: ≈ 393 s). Com o circuito aberto a chamada falha em tempo
zero. Os números vêm de `iped_mcp.config` (env), nunca de literais no fluxo.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from iped_mcp.config import (
    CB_COOLDOWN_SECONDS,
    CB_FAILURE_THRESHOLD,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_CAP,
    RETRY_MAX_ATTEMPTS,
)
from iped_mcp.errors import (
    IpedConnectionError,
    IpedHTTPError,
    IpedTimeoutError,
    ServerError,
)

logger = logging.getLogger("iped_mcp.resilience")

# Único verbo re-tentável em resposta ambígua; POST/PUT/DELETE são mutações.
_IDEMPOTENT_METHOD = "GET"


class RetryPolicy:
    """Decide se uma falha é transitória (re-tentável) e quanto esperar.

    A decisão é por classe da taxonomia M-2, nunca por mensagem de texto.
    """

    def __init__(
        self,
        max_attempts: int = RETRY_MAX_ATTEMPTS,
        backoff_base: float = RETRY_BACKOFF_BASE,
        backoff_cap: float = RETRY_BACKOFF_CAP,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Parametriza a política.

        Args:
            max_attempts: tentativas totais por chamada (>= 1).
            backoff_base: base do backoff exponencial (espera = base**tentativa).
            backoff_cap: teto de segundos de uma única espera.
            sleep: função de espera (injetável em teste).
        """
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.sleep = sleep

    def should_retry(
        self,
        error: BaseException,
        method: str,
        *,
        retry_server_errors: bool = True,
    ) -> bool:
        """A falha `error` da requisição `method` deve ser re-tentada?

        Args:
            error: exceção da taxonomia `iped_mcp.errors` capturada.
            method: verbo HTTP da requisição ("GET", "POST", "PUT", "DELETE").
            retry_server_errors: False em rotas cujo 5xx é determinístico
                (ex.: `/search`, onde 500 = query rejeitada → re-tentar só
                desperdiça tempo; AC4 do DESIGN 03).

        Returns:
            True se a tentativa deve ser repetida com backoff.
        """
        if isinstance(error, IpedHTTPError):
            if not isinstance(error, ServerError):
                return False  # 4xx: erro do cliente, nunca (contrato M-2)
            return retry_server_errors and method == _IDEMPOTENT_METHOD
        if isinstance(error, IpedTimeoutError):
            # Timeout em mutação = resposta ambígua (pode ter processado).
            return method == _IDEMPOTENT_METHOD
        if isinstance(error, IpedConnectionError):
            # Sem conexão não houve resposta: seguro mesmo em mutação.
            return True
        return False  # fora da taxonomia de transporte: não re-tenta

    def backoff(self, attempt: int) -> float:
        """Segundos a aguardar antes da tentativa `attempt + 2` (0-indexado).

        Args:
            attempt: índice da tentativa que acabou de falhar (0, 1, ...).

        Returns:
            min(backoff_base ** attempt, backoff_cap).
        """
        return min(self.backoff_base**attempt, self.backoff_cap)


class CircuitBreaker:
    """Circuit breaker por base_url: abre após N falhas de conexão seguidas.

    Estados (implementados por contador + timestamp, thread-safe):

    - FECHADO: chamadas passam; falhas de conexão de nível-de-chamada
      (retries já esgotados) acumulam; sucesso ou QUALQUER resposta HTTP
      (inclusive 4xx/5xx — o servidor respondeu = rede viva) zeram o
      contador;
    - ABERTO: falha rápida com mensagem que nomeia o endpoint e a janela.
      Ao atingir o limiar, e de novo quando a janela expira, uma sonda
      (probe) decide: sonda OK → FECHADO (limiar atingido: "operação
      lenta", não pane; janela expirada: servidor voltou); sonda falhou →
      mais uma janela ABERTA.

    A sonda é fornecida pelo dono do circuito (o client faz `GET /sources/`)
    e NUNCA roda sob o lock — rede não bloqueia outros threads.
    """

    def __init__(
        self,
        name: str,
        probe: Callable[[], bool],
        failure_threshold: int = CB_FAILURE_THRESHOLD,
        cooldown_s: float = CB_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Parametriza o circuito.

        Args:
            name: identificação do endpoint (base_url) para a mensagem.
            probe: sonda de vida (True = endpoint respondeu).
            failure_threshold: falhas de conexão consecutivas p/ abrir.
            cooldown_s: segundos com o circuito aberto antes da próxima sonda.
            clock: relógio monotônico (injetável em teste).
        """
        self._name = name
        self._probe = probe
        self._failure_threshold = max(2, int(failure_threshold))
        self._cooldown_s = float(cooldown_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._consec_fail = 0
        self._open_until: Optional[float] = None

    @property
    def is_open(self) -> bool:
        """O circuito está aberto (janela de fail-fast ativa)?"""
        with self._lock:
            return self._open_until is not None and self._clock() < self._open_until

    def guard(self) -> None:
        """Barreira de entrada de toda chamada: fail-fast se ABERTO.

        Raises:
            IpedConnectionError: circuito aberto (dentro da janela, ou a
                sonda pós-janela voltou a falhar).
        """
        with self._lock:
            if self._open_until is None:
                return
            now = self._clock()
            if now < self._open_until:
                raise IpedConnectionError(self._open_message(now), url=self._name)
        # Janela expirou: a sonda decide a reabertura, fora do lock.
        if self._probe():
            with self._lock:
                self._reset()
            logger.info("Sonda OK em %s — circuito fechado.", self._name)
            return
        with self._lock:
            self._open_until = self._clock() + self._cooldown_s
            now = self._clock()
        raise IpedConnectionError(self._open_message(now), url=self._name)

    def record_success(self) -> None:
        """Resposta do servidor (qualquer status): rede viva → fecha/zera."""
        with self._lock:
            self._reset()

    def record_connection_failure(self) -> None:
        """Registra uma chamada cuja falha foi de conexão (retries esgotados).

        No limiar, a sonda decide entre abrir (servidor morto) e continuar
        fechado contando "operação lenta" — nunca derruba a triagem inteira
        por um doc pesado acima do timeout.
        """
        with self._lock:
            self._consec_fail += 1
            reached = self._consec_fail >= self._failure_threshold
            if not reached:
                return
        if self._probe():
            with self._lock:
                self._reset()
            logger.warning(
                "Falhas de conexão em %s mas a sonda respondeu: servidor "
                "vivo, operação lenta — o circuito NÃO abre.",
                self._name,
            )
            return
        with self._lock:
            self._open_until = self._clock() + self._cooldown_s
            failures = self._consec_fail
        logger.error(
            "%s inacessível (%d falhas de conexão seguidas, sonda também "
            "falhou) — circuito aberto por %.0fs.",
            self._name,
            failures,
            self._cooldown_s,
        )

    def _reset(self) -> None:
        """Volta ao estado FECHADO (zerar contador e janela)."""
        self._consec_fail = 0
        self._open_until = None

    def _open_message(self, now: float) -> str:
        """Mensagem AC2: nomeia o endpoint e a janela restante."""
        remaining = max(0.0, (self._open_until or now) - now)
        return (
            f"WebAPI indisponível (circuito aberto): {self._name} falhou "
            f"{self._failure_threshold}× seguidas; sonda de reabertura em "
            f"{remaining:.0f}s."
        )
