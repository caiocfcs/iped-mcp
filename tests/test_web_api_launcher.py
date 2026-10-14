"""Testes do web-api-launcher.py (M-5 — bind padrao 127.0.0.1).

Cobre o Design 05: default loopback (AC1), aviso explicito ao expor com
risco nomeado (AC2) e preservacao da montagem do argv do JVM (AC4). O
modulo e carregado por caminho porque o nome do arquivo contem hifen e
o launcher nao e um pacote importavel.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "web-api-launcher.py"


def load_launcher_module() -> ModuleType:
    """Carrega o script do launcher como modulo independente.

    Returns:
        Modulo ``web_api_launcher`` executado a partir do arquivo.

    Raises:
        RuntimeError: se o loader nao conseguir executar o arquivo.
    """
    spec = importlib.util.spec_from_file_location("web_api_launcher", LAUNCHER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"nao foi possivel carregar {LAUNCHER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["web_api_launcher"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def launcher() -> ModuleType:
    """Instancia unica do modulo do launcher para este arquivo de testes."""
    return load_launcher_module()


def test_default_host_e_loopback(launcher: ModuleType) -> None:
    """AC1: o default do launcher nao expoe a WebAPI a rede."""
    assert launcher.DEFAULT_HOST == "127.0.0.1"


def test_parse_args_sem_flags_usa_loopback(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: sem flags, --host resolve para o default 127.0.0.1."""
    monkeypatch.setattr(sys, "argv", ["web-api-launcher.py"])
    assert launcher.parse_args().host == "127.0.0.1"


def test_parse_args_flag_host_preserva_exposicao_explicita(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: --host externo continua permitido, porem como escolha explicita."""
    monkeypatch.setattr(sys, "argv", ["web-api-launcher.py", "--host", "0.0.0.0"])
    assert launcher.parse_args().host == "0.0.0.0"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.5.6.7", True),
        ("localhost", True),
        ("LOCALHOST", True),
        (" ::1 ", True),
        ("0.0.0.0", False),
        ("192.168.1.10", False),
        ("::", False),
        ("", False),
    ],
)
def test_is_loopback_host(launcher: ModuleType, host: str, expected: bool) -> None:
    """Classificacao de loopback cobre 127/8, localhost e ::1."""
    assert launcher.is_loopback_host(host) is expected


def test_warn_if_exposed_nomeia_o_risco(launcher: ModuleType, capsys) -> None:
    """AC2: exposicao externa emite aviso visivel nomeando a falta de auth."""
    launcher.warn_if_exposed("0.0.0.0")
    err = capsys.readouterr().err
    assert "AVISO" in err
    assert "sem autenticacao" in err
    assert "0.0.0.0" in err


def test_warn_if_exposed_silencioso_em_loopback(
    launcher: ModuleType, capsys
) -> None:
    """AC2: bind loopback (default) nao imprime aviso."""
    launcher.warn_if_exposed("127.0.0.1")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_build_server_command_argv_padrao(launcher: ModuleType) -> None:
    """AC1/AC4: argv do JVM pinado — sem flags o JAR recebe 127.0.0.1."""
    command = launcher.build_server_command(
        Path("/iped/jre/bin/java"),
        Path("/iped/lib/iped-webapi.jar"),
        launcher.DEFAULT_HOST,
        1234,
        Path("/tmp/source-1.json"),
    )
    assert command == [
        "/iped/jre/bin/java",
        "-jar",
        "/iped/lib/iped-webapi.jar",
        "--host=127.0.0.1",
        "--port=1234",
        "--sources=/tmp/source-1.json",
    ]


def test_build_server_command_repassa_host_exposto(launcher: ModuleType) -> None:
    """AC4: host externo explicito segue inalterado para o JAR."""
    command = launcher.build_server_command(
        Path("java"), Path("api.jar"), "0.0.0.0", 1240, Path("s.json")
    )
    assert "--host=0.0.0.0" in command
    assert "--port=1240" in command
