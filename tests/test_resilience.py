"""Testes da política de resiliência M-3 (retry ×3 + circuit breaker).

Sem rede real em nenhum teste: falhas injetadas por sessão fake roteada
pelo client de produção (`FakeSession`) e, nos fluxos ponta a ponta, o
fake HTTP do conftest (M-1). A prova de AC1/AC4 é por CONTAGEM de
requisições (log da sessão ou `webapi.requests_log`).
"""

from __future__ import annotations

import json
from typing import Any, Callable

import pytest
import requests

from iped_mcp import fetch
from iped_mcp import router as router_mod
from iped_mcp.client import IPEDClient
from iped_mcp.config import CB_PROBE_TIMEOUT_SECONDS
from iped_mcp.errors import (
    BadRequestError,
    IpedConnectionError,
    IpedResponseError,
    IpedTimeoutError,
    ResourceNotFoundError,
    ServerError,
)
from iped_mcp.resilience import CircuitBreaker, RetryPolicy
from tests.conftest import (
    ALL_DOC_IDS,
    MISSING_DOC_ID,
    SOURCE_ID,
    UNKNOWN_SOURCE,
)


# --- infraestrutura fake ------------------------------------------------------


class FakeClock:
    """Relógio monotônico controlado pelo teste (`advance` move o tempo)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_response(status: int, payload: Any = None) -> requests.Response:
    """Resposta mínima do requests (corpo JSON p/ <400; HTML Grizzly p/ 5xx)."""
    resp = requests.Response()
    resp.status_code = status
    resp.url = "http://fake-iped/x"
    if status < 400:
        body = {"data": payload if payload is not None else []}
        resp._content = json.dumps(body).encode("utf-8")
    elif status >= 500:
        resp._content = b"<html>Grizzly Framework 2.3.31</html>"
    else:
        resp._content = b"not found"
    return resp


class FakeSession:
    """Sessão scripted: cada request consome um item (resposta ou exceção)."""

    def __init__(self, items: list[Any]) -> None:
        self.items = list(items)
        self.calls: list[dict[str, Any]] = []

    def extend(self, items: list[Any]) -> None:
        """Anexa novos itens ao fim do script (testes de múltiplas fases)."""
        self.items.extend(items)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.items:
            raise AssertionError(f"request não scripted: {method} {url}")
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def scripted_client(
    items: list[Any], start: float = 1000.0
) -> tuple[IPEDClient, FakeSession, FakeClock, list[float]]:
    """Client de produção com sessão/reloginho/espera injetados (sem rede).

    Returns:
        (client, sessão fake, relógio fake, esperas de backoff registradas).
    """
    clock = FakeClock(start)
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    session = FakeSession(items)
    client = IPEDClient("http://fake-iped", session=session, sleeper=sleeper, clock=clock)
    return client, session, clock, sleeps


def make_breaker(
    probe_ok: list[bool], clock: FakeClock | None = None
) -> tuple[CircuitBreaker, FakeClock, list[int]]:
    """Breaker com sonda controlável; `probe_ok[0]` é o veredito atual."""
    clock = clock or FakeClock()
    probe_calls: list[int] = []

    def probe() -> bool:
        probe_calls.append(1)
        return probe_ok[0]

    breaker = CircuitBreaker(name="http://fake-iped", probe=probe, clock=clock)
    return breaker, clock, probe_calls


# --- RetryPolicy (unidade) -----------------------------------------------------


class TestRetryPolicyDecisoes:
    """Matriz de decisão por classe da taxonomia M-2 (AC1)."""

    @pytest.mark.parametrize("error", [
        BadRequestError("recusada", status_code=400),
        ResourceNotFoundError("não existe", status_code=404),
    ])
    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
    def test_4xx_nunca_e_retryado(self, error: Exception, method: str) -> None:
        assert not RetryPolicy().should_retry(error, method)

    @pytest.mark.parametrize("error", [
        IpedConnectionError("offline"),
        IpedTimeoutError("devagar"),
    ])
    def test_rede_retryavel_em_get(self, error: Exception) -> None:
        assert RetryPolicy().should_retry(error, "GET")

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
    def test_mutacao_so_conexao_e_retryavel(self, method: str) -> None:
        """Timeout/5xx em mutação = resposta ambígua → nunca (cautela bookmark)."""
        policy = RetryPolicy()
        assert policy.should_retry(IpedConnectionError("offline"), method)
        assert not policy.should_retry(IpedTimeoutError("devagar"), method)
        assert not policy.should_retry(ServerError("má", status_code=500), method)

    def test_5xx_retryavel_em_get(self) -> None:
        error = ServerError("interno", status_code=503)
        assert RetryPolicy().should_retry(error, "GET")

    def test_search_500_nao_e_retryavel(self) -> None:
        """AC4: 500 de /search é determinístico (query rejeitada)."""
        error = ServerError("query inválida", status_code=500)
        assert not RetryPolicy().should_retry(
            error, "GET", retry_server_errors=False
        )

    @pytest.mark.parametrize("error", [
        IpedResponseError("sem texto"),
        ValueError("validação"),
    ])
    def test_fora_da_taxonomia_nao_retrya(self, error: Exception) -> None:
        assert not RetryPolicy().should_retry(error, "GET")

    def test_backoff_exponencial_com_teto(self) -> None:
        policy = RetryPolicy(max_attempts=3, backoff_base=2, backoff_cap=8)
        assert policy.backoff(0) == 1.0
        assert policy.backoff(1) == 2.0
        assert policy.backoff(2) == 4.0
        assert policy.backoff(10) == 8.0  # teto


# --- CircuitBreaker (unidade) ---------------------------------------------------


class TestCircuitBreaker:
    """AC2: 4 falhas abrem; fail-fast na janela; sonda reabre."""

    def test_quatro_falhas_abrem_e_tres_mantem_fechado(self) -> None:
        probe_ok = [False]
        breaker, _clock, probe_calls = make_breaker(probe_ok)
        for _ in range(3):
            breaker.record_connection_failure()
        assert not breaker.is_open
        assert probe_calls == []  # abaixo do limiar não sonda
        breaker.record_connection_failure()
        assert breaker.is_open
        assert probe_calls == [1]  # no limiar, a sonda decidiu abrir
        with pytest.raises(IpedConnectionError, match="circuito aberto"):
            breaker.guard()

    def test_mensagem_nomeia_endpoint_e_janela(self) -> None:
        probe_ok = [False]
        breaker, _clock, _calls = make_breaker(probe_ok)
        for _ in range(4):
            breaker.record_connection_failure()
        with pytest.raises(IpedConnectionError) as excinfo:
            breaker.guard()
        assert "http://fake-iped" in str(excinfo.value)
        assert "60" in str(excinfo.value)  # janela nomeada (mitigação AC2)

    def test_dentro_da_janela_fail_fast_sem_sonda(self) -> None:
        probe_ok = [False]
        breaker, clock, probe_calls = make_breaker(probe_ok)
        for _ in range(4):
            breaker.record_connection_failure()
        probe_calls.clear()
        clock.advance(59)
        with pytest.raises(IpedConnectionError, match="circuito aberto"):
            breaker.guard()
        assert probe_calls == []  # fail-fast puro: zero rede

    def test_janela_expirada_sonda_ok_reabre(self) -> None:
        probe_ok = [False]
        breaker, clock, probe_calls = make_breaker(probe_ok)
        for _ in range(4):
            breaker.record_connection_failure()
        clock.advance(61)
        probe_ok[0] = True
        breaker.guard()  # sonda decide: servidor voltou
        assert not breaker.is_open
        breaker.guard()  # fechado: passa direto, sem nova sonda
        assert probe_calls == [1, 1]  # abertura + reabertura

    def test_janela_expirada_sonda_falha_mantem_aberto(self) -> None:
        probe_ok = [False]
        breaker, clock, _calls = make_breaker(probe_ok)
        for _ in range(4):
            breaker.record_connection_failure()
        clock.advance(61)
        with pytest.raises(IpedConnectionError, match="circuito aberto"):
            breaker.guard()
        assert breaker.is_open

    def test_sonda_ok_no_limiar_e_operacao_lenta_nao_abre(self) -> None:
        """Doc pesado >timeout não derruba a triagem inteira (icaro :109-124)."""
        probe_ok = [True]
        breaker, _clock, _calls = make_breaker(probe_ok)
        for _ in range(10):
            breaker.record_connection_failure()
        assert not breaker.is_open

    def test_resposta_http_zera_contador(self) -> None:
        """5xx = servidor vivo: não pontua o breaker e reseta a sequência."""
        probe_ok = [False]
        breaker, _clock, probe_calls = make_breaker(probe_ok)
        for _ in range(3):
            breaker.record_connection_failure()
        breaker.record_success()  # uma resposta (qualquer status) reseta
        for _ in range(3):
            breaker.record_connection_failure()
        assert not breaker.is_open
        assert probe_calls == []


# --- IPEDClient integrado à política --------------------------------------------


class TestClientRetry:
    """Retry no ponto único de saída do client — por classe de erro (AC1)."""

    def test_5xx_get_retryado_ate_sucesso_com_backoff(self) -> None:
        items = [make_response(500), make_response(500), make_response(200)]
        client, session, _clock, sleeps = scripted_client(items)
        assert client.list_sources() == []
        assert len(session.calls) == 3  # AC1: superada em ≤3 tentativas
        assert sleeps == [1.0, 2.0]  # backoff 2^n entre tentativas

    def test_4xx_sobe_na_hora_sem_retry(self) -> None:
        client, session, _clock, sleeps = scripted_client([make_response(404)])
        with pytest.raises(ResourceNotFoundError):
            client.list_sources()
        assert len(session.calls) == 1
        assert sleeps == []

    def test_falha_de_conexao_esgota_tres_tentativas(self) -> None:
        items = [requests.ConnectionError("morta") for _ in range(3)]
        client, session, _clock, sleeps = scripted_client(items)
        with pytest.raises(IpedConnectionError):
            client.list_sources()
        assert len(session.calls) == 3
        assert sleeps == [1.0, 2.0]
        assert not client._breaker.is_open  # 1 chamada ≠ circuito aberto

    def test_timeout_get_e_retryado(self) -> None:
        items = [requests.Timeout("lento"), make_response(200)]
        client, session, _clock, _sleeps = scripted_client(items)
        assert client.list_sources() == []
        assert len(session.calls) == 2

    def test_search_500_imediato_sem_retry(self) -> None:
        """AC4: query rejeitada falha na hora (mesmo client sem o fetch)."""
        client, session, _clock, sleeps = scripted_client([make_response(500)])
        with pytest.raises(ServerError):
            client.search("query-invalida(", source_id="x")
        assert len(session.calls) == 1
        assert sleeps == []

    def test_mutacao_timeout_nao_retrya_resposta_ambigua(self) -> None:
        client, session, _clock, _sleeps = scripted_client(
            [requests.Timeout("ambíguo")]
        )
        with pytest.raises(IpedTimeoutError):
            client.add_to_bookmark("bm", [{"source": "s", "id": 1}])
        assert len(session.calls) == 1

    def test_mutacao_falha_de_conexao_e_retryada(self) -> None:
        client, session, _clock, _sleeps = scripted_client(
            [requests.ConnectionError("recusada"), make_response(200)]
        )
        client.create_bookmark("bm")  # idempotente no jar (PB4/PB5)
        assert len(session.calls) == 2


class TestClientCircuitBreaker:
    """Integração breaker×client: abertura, fail-fast e reabertura via sonda."""

    def _dead_items(self, n_calls: int) -> list[Any]:
        return [requests.ConnectionError("morta") for _ in range(3 * n_calls)]

    def test_abre_apos_4_chamadas_e_falha_rapido(self) -> None:
        # 4ª chamada: 3 tentativas + 1 sonda (a sonda decide abrir)
        items = self._dead_items(3) + self._dead_items(1) + [requests.ConnectionError("sonda")]
        client, session, clock, _sleeps = scripted_client(items)
        for _ in range(4):
            with pytest.raises(IpedConnectionError):
                client.list_sources()
        assert client._breaker.is_open
        n_calls = len(session.calls)
        with pytest.raises(IpedConnectionError, match="circuito aberto"):
            client.list_sources()
        assert len(session.calls) == n_calls  # fail-fast: zero rede

    def test_sonda_pos_janela_reabre_e_chamada_passa(self) -> None:
        items = self._dead_items(4) + [requests.ConnectionError("sonda")]
        client, session, clock, _sleeps = scripted_client(items)
        for _ in range(4):
            with pytest.raises(IpedConnectionError):
                client.list_sources()
        clock.advance(61)
        session.extend([make_response(200), make_response(200)])  # sonda + chamada
        assert client.list_sources() == []
        assert not client._breaker.is_open
        probe_call = session.calls[-2]
        assert probe_call["url"].endswith("/sources/")
        assert probe_call["kwargs"]["timeout"] == CB_PROBE_TIMEOUT_SECONDS

    def test_5xx_nao_pontua_o_breaker(self) -> None:
        """500 persistente = servidor vivo; não pode abrir o circuito."""
        items = self._dead_items(3)  # 3 chamadas com falha de conexão
        client, session, _clock, _sleeps = scripted_client(items)
        for _ in range(3):
            with pytest.raises(IpedConnectionError):
                client.list_sources()
        client._breaker.record_success()  # resposta HTTP no meio da sequência
        session.extend(self._dead_items(3))
        for _ in range(3):
            with pytest.raises(IpedConnectionError):
                client.list_sources()
        assert not client._breaker.is_open  # nunca chegou a 4 consecutivas


# --- contra o fake WebAPI (M-1): contagem no servidor ----------------------------


class FlakySession:
    """Envolve uma sessão real; falha as primeiras requisições scripted."""

    def __init__(self, inner: requests.Session, failures: list[Exception]) -> None:
        self.inner = inner
        self.failures = list(failures)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if self.failures:
            raise self.failures.pop(0)
        return self.inner.request(method, url, **kwargs)


class TestPoliticaNoFakeWebAPI:
    """AC1/AC3 ponta a ponta: contagem de requisições no servidor fake."""

    def test_falha_transitoria_de_rede_e_superada(self, webapi: Any) -> None:
        session = FlakySession(
            requests.Session(),
            [requests.ConnectionError("piscada"), requests.Timeout("piscada")],
        )
        client = IPEDClient(webapi.base_url, session=session)
        sources = client.list_sources()
        assert SOURCE_ID in [s["id"] for s in sources]
        # só a requisição bem-sucedida chegou ao servidor
        assert len(webapi.requests_log) == 1

    def test_5xx_persistente_recebe_tres_tentativas(self, webapi: Any) -> None:
        client = IPEDClient(webapi.base_url)
        with pytest.raises(ServerError):
            client.get_document(SOURCE_ID, MISSING_DOC_ID)
        assert len(webapi.requests_log) == 3

    def test_404_uma_unica_requisicao(self, webapi: Any) -> None:
        """Source inexistente = 404 (ResourceNotFoundError): nunca retrya."""
        client = IPEDClient(webapi.base_url)
        with pytest.raises(ResourceNotFoundError):
            client.search("*", source_id=UNKNOWN_SOURCE)
        assert len(webapi.requests_log) == 1


# --- fetch.py delega o retry ao client (AC4) --------------------------------------


class TestFetchDelegaResiliencia:
    """O 500-imediato do search e o contrato None do fetch continuam iguais."""

    def test_search_ids_500_diagnostico_imediato_uma_chamada(
        self, webapi: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1º item: sonda/registry do Router na construção; 2º: o /search 500.
        session = FakeSession(
            [make_response(200, webapi.sources), make_response(500)]
        )
        client = IPEDClient(webapi.base_url, session=session)
        router = router_mod.Router(
            {"default": webapi.base_url}, clients={"default": client}
        )
        monkeypatch.setattr(router_mod, "_router", router)
        with pytest.raises(ValueError, match="HTTP 500"):
            fetch.search_ids("query-ruim(", SOURCE_ID, "hint de teste")
        search_calls = [c for c in session.calls if "/search" in c["url"]]
        assert len(search_calls) == 1  # AC4: sem ×3 no 500 de query

    def test_search_ids_achata_ids_do_servidor(
        self, fake_router: Any, webapi: Any
    ) -> None:
        assert fetch.search_ids("*", SOURCE_ID, "hint") == list(ALL_DOC_IDS)

    def test_get_document_props_uma_chamada_por_doc(
        self, fake_router: Any, webapi: Any
    ) -> None:
        """Fetch não multiplica o retry do client (1 chamada, não 3×3)."""
        session = FakeSession([make_response(500)] * 3)
        fake_router._clients["default"] = IPEDClient(
            webapi.base_url, session=session
        )
        assert fetch.get_document_props(SOURCE_ID, MISSING_DOC_ID) is None
        assert len(session.calls) == 3  # política do client; fetch single-shot

    def test_texto_falha_de_conexao_vira_none_sem_envenenar_cache(
        self, fake_router: Any, webapi: Any
    ) -> None:
        """M-8: continua None apenas a FALHA de verdade (rede); cache limpo.

        Rede é transitória: o retry do M-3 continua valendo (3 tentativas,
        sleeper injetado); o estado no_text é só para o 500 do jar."""
        session = FakeSession([requests.ConnectionError("offline")] * 3)
        fake_router._clients["default"] = IPEDClient(
            webapi.base_url, session=session, sleeper=lambda _s: None
        )
        assert fetch.get_document_text_cached(SOURCE_ID, MISSING_DOC_ID) is None
        assert fetch.text_cache_get(SOURCE_ID, MISSING_DOC_ID) is None
        assert len(session.calls) == 3  # retry de conexão preservado (M-3)

    def test_texto_500_vira_dado_uma_sochamada_e_cacheavel(
        self, fake_router: Any, webapi: Any
    ) -> None:
        """M-8: 500 de /text e estado (no_text), 1 chamada (rota deterministica)
        e entra no cache — o contrato WONT_FIX P2-5 nao e mais 'falha'."""
        session = FakeSession([make_response(500)])
        fake_router._clients["default"] = IPEDClient(
            webapi.base_url, session=session
        )
        result = fetch.get_document_text_cached(SOURCE_ID, MISSING_DOC_ID)
        assert result is not None and result.status == "no_text"
        assert len(session.calls) == 1  # retry_server_errors=False em /text
        assert fetch.text_cache_get(SOURCE_ID, MISSING_DOC_ID) is result
