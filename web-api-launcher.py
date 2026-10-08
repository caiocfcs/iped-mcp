#!/usr/bin/env python3
"""Sobe uma instancia da IPED Web API para cada source de um multicases.json."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 1234


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sobe uma instancia da IPED Web API para cada source do multicases.json."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Pasta que contem jre/ e lib/ (padrao: diretorio atual).",
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=Path("multicases.json"),
        help="Arquivo multicases.json (padrao: ./multicases.json).",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Endereco de escuta (padrao: {DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--start-port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Primeira porta a testar (padrao: {DEFAULT_PORT}).",
    )
    return parser.parse_args()


def load_sources(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(f"ERRO: arquivo nao encontrado: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERRO: JSON invalido em {path}: {exc}")

    if not isinstance(data, list) or not data:
        raise SystemExit("ERRO: o multicases.json deve conter uma lista JSON nao vazia.")

    for index, source in enumerate(data, start=1):
        if not isinstance(source, dict):
            raise SystemExit(f"ERRO: source #{index} nao e um objeto JSON.")
        if not source.get("id") or not source.get("path"):
            raise SystemExit(f"ERRO: source #{index} deve conter 'id' e 'path'.")
    return data


def port_is_available(host: str, port: int) -> bool:
    if not 1 <= port <= 65535:
        return False
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        return True
    except OSError:
        return False


def next_available_port(host: str, candidate: int, reserved: set[int]) -> int:
    while candidate <= 65535:
        if candidate not in reserved and port_is_available(host, candidate):
            return candidate
        print(f"[PORTA] {candidate} ocupada ou indisponivel; tentando {candidate + 1}.")
        candidate += 1
    raise SystemExit("ERRO: nenhuma porta disponivel ate 65535.")


class WindowsJob:
    """Job Object que encerra os filhos quando o processo Python fecha."""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount"
            )]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32
        )
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())

        self.handle = handle
        self._kernel32 = kernel32

    def assign(self, process: subprocess.Popen[Any]) -> None:
        if self.handle is not None:
            if not self._kernel32.AssignProcessToJobObject(
                self.handle, ctypes.c_void_p(process._handle)
            ):
                raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle is not None:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


class ServerManager:
    def __init__(self, job: WindowsJob) -> None:
        self.processes: list[tuple[dict[str, Any], int, subprocess.Popen[Any]]] = []
        self.job = job
        self.stopping = False

    def add(self, source: dict[str, Any], port: int, process: subprocess.Popen[Any]) -> None:
        self.processes.append((source, port, process))

    def stop_all(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        print("\n[ENCERRANDO] Derrubando todos os servidores IPED...")

        # No Windows, fechar o Job Object mata toda a arvore de processos.
        if os.name == "nt":
            self.job.close()
        else:
            for _, _, proc in self.processes:
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

        deadline = time.monotonic() + 5
        for _, _, proc in self.processes:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        proc.kill()
                    else:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
        print("[ENCERRADO] Todos os processos foram finalizados.")


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    sources_file = args.sources.expanduser()
    if not sources_file.is_absolute():
        # O caminho de --sources e relativo ao diretorio de execucao, como solicitado.
        sources_file = sources_file.resolve()

    java = root / "jre" / "bin" / ("java.exe" if os.name == "nt" else "java")
    jar = root / "lib" / "iped-webapi.jar"
    if not java.is_file():
        raise SystemExit(f"ERRO: Java nao encontrado: {java}")
    if not jar.is_file():
        raise SystemExit(f"ERRO: JAR nao encontrado: {jar}")
    if not 1 <= args.start_port <= 65535:
        raise SystemExit("ERRO: --start-port deve estar entre 1 e 65535.")

    sources = load_sources(sources_file)
    job = WindowsJob()
    manager = ServerManager(job)

    def request_shutdown(signum: int, _frame: Any) -> None:
        print(f"\n[SINAL] Recebido sinal {signum}.")
        manager.stop_all()
        raise SystemExit(0)

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, request_shutdown)

    temp_dir = Path(tempfile.mkdtemp(prefix="iped-webapi-"))
    reserved: set[int] = set()
    candidate = args.start_port

    try:
        for index, source in enumerate(sources, start=1):
            port = next_available_port(args.host, candidate, reserved)
            reserved.add(port)
            candidate = port + 1

            single_source = temp_dir / f"source-{index}.json"
            single_source.write_text(
                json.dumps([source], ensure_ascii=False, indent=2), encoding="utf-8"
            )

            command = [
                str(java), "-jar", str(jar),
                f"--host={args.host}", f"--port={port}",
                f"--sources={single_source}",
            ]
            popen_options: dict[str, Any] = {"cwd": str(root)}
            if os.name == "nt":
                popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_options["start_new_session"] = True

            print(f"[INICIANDO] source={source['id']} porta={port}")
            proc = subprocess.Popen(command, **popen_options)
            try:
                job.assign(proc)
            except Exception:
                proc.terminate()
                raise
            manager.add(source, port, proc)

            # Detecta falhas imediatas, sem presumir que a API possui endpoint de health check.
            time.sleep(0.35)
            if proc.poll() is not None:
                raise RuntimeError(
                    f"A API da source {source['id']} encerrou imediatamente "
                    f"com codigo {proc.returncode}."
                )

        print("\n=== SERVIDORES IPED INICIADOS ===")
        for source, port, proc in manager.processes:
            print(f"- {source['id']}: http://{args.host}:{port}/  (PID {proc.pid})")
        print("\nPressione Ctrl+C para encerrar todos os servidores.")

        while True:
            for source, port, proc in manager.processes:
                code = proc.poll()
                if code is not None:
                    raise RuntimeError(
                        f"Servidor da source {source['id']} na porta {port} "
                        f"encerrou com codigo {code}."
                    )
            time.sleep(1)

    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"\nERRO: {exc}", file=sys.stderr)
        return 1
    finally:
        manager.stop_all()
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
