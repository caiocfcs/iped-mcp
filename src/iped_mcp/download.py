"""Helpers compartilhados de nome e escrita de exports do IPED (M-10).

Fonte única dos pontos de gravação de artefatos (core, bookmarks, regex,
whatsapp — DESIGN 10/AC6): o nome segue `<UTC-ts>_<source>_<safe_name><ext>`,
a reserva do arquivo é atômica (`O_CREAT|O_EXCL`) e uma reexecução NUNCA
sobrescreve o artefato anterior — colisão ganha sufixo incremental `-N`.
A extensão segue o Content-Type real do /content (fork P1-3) quando existe
mapa; sem mapa (ou com `application/octet-stream`) vale a extensão do nome
original.
"""

from __future__ import annotations

import mimetypes
import os
import re
from datetime import datetime, timezone

import requests

from iped_mcp import config

# Timestamp NO NOME DO ARQUIVO: compacto, ordenável lexicograficamente e sem
# separadores proibidos; o sufixo `Z` declara UTC (regex/whatsapp usavam hora
# LOCAL antes do M-10 — D-06/WI-22).
EXPORT_TIMESTAMP_FMT = "%Y%m%dT%H%M%SZ"
# Formato ISO-UTC dos campos de data DENTRO dos artefatos (generated_at etc.).
ISO_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
# octet-stream é "tipo desconhecido": nunca dita extensão (senão o mimetypes
# produziria `.bin` em todo export genérico).
GENERIC_CONTENT_TYPE = "application/octet-stream"
# Corrigenda do mimetypes padrão do CPython: guess_extension devolve `.jpe`.
EXTENSION_FIXUPS = {".jpe": ".jpg"}
# Bytes por chunk no streaming de /content (mesmo teto de core/bookmarks
# pré-M-10, agora centralizado aqui).
CHUNK_SIZE = 1024 * 1024  # 1 MB

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SAFE_NAME_FALLBACK = "export"


def utc_now() -> datetime:
    """Instante atual em UTC com timezone (fonte única de tempo dos exports).

    Returns:
        `datetime` aware em UTC — usar com `export_path(now=...)` e `iso_utc`
        para nome e metadados saírem do MESMO instante.
    """
    return datetime.now(timezone.utc)


def iso_utc(moment: datetime) -> str:
    """Formata `moment` como ISO-UTC `YYYY-MM-DDTHH:MM:SSZ` (campos internos).

    Args:
        moment: instante (naive é interpretado como UTC pelo contrato do
            helper — quem produz o timestamp chama `utc_now`).

    Returns:
        String ISO terminando em `Z`.
    """
    return moment.strftime(ISO_UTC_FMT)


def safe_name(name: object, fallback: str = _SAFE_NAME_FALLBACK) -> str:
    """Nome seguro para filename: chars inválidos em `_`; vazio → `fallback`.

    Args:
        name: texto bruto (nome de bookmark, source, nome de arquivo).
        fallback: resultado quando nada sobrevive à limpeza.

    Returns:
        String sem separadores de path/chars de controle, não vazia.
    """
    cleaned = _UNSAFE_CHARS.sub("_", str(name)).strip()
    return cleaned if cleaned.strip("_") else fallback


def resolve_export_dir(dest_dir: str | None) -> str:
    """Destino efetivo de export: o informado (abs/expandido) ou o config.

    Args:
        dest_dir: pasta passada pela tool; None/vazio usa
            `config.EXPORTS_DIR` (`IPED_MCP_EXPORTS_DIR`, fonte única M-10).

    Returns:
        Caminho absoluto (o diretório em si é criado por `export_path`).
    """
    raw = dest_dir or config.EXPORTS_DIR
    return os.path.abspath(os.path.expanduser(raw))


def extension_for_content_type(content_type: str | None) -> str:
    """Extensão inferida do Content-Type real (P1-3); `` se não houver mapa.

    Args:
        content_type: header cru (pode trazer parâmetros `; charset=...`).

    Returns:
        Extensão com ponto (ex.: `.pdf`) ou `` para ausente/desconhecido/
        octet-stream — nesse caso o chamador mantém a extensão do nome.
    """
    if not content_type:
        return ""
    media = content_type.split(";", 1)[0].strip().lower()
    if not media or media == GENERIC_CONTENT_TYPE:
        return ""
    ext = mimetypes.guess_extension(media) or ""
    return EXTENSION_FIXUPS.get(ext, ext)


def pick_export_name(doc_id: int, header_filename: str, doc_name: str | None) -> str:
    """Escolha do nome-base de um doc: header → prop `name` → `doc_{id}`.

    Args:
        doc_id: id do documento (fallback final de identificação).
        header_filename: filename do Content-Disposition (`` quando ausente).
        doc_name: prop `name` do doc (None quando indisponível).

    Returns:
        Somente o basename (sem diretórios); a sanitização/sufixo ficam com
        `export_path`.
    """
    base = header_filename or doc_name or f"doc_{doc_id}"
    return os.path.basename(str(base)) or f"doc_{doc_id}"


def _ensure_export_dir(dest_dir: str) -> None:
    """Cria o diretório de export; ValueError com diagnóstico se falhar."""
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            f"Não foi possível criar o diretório de exportação '{dest_dir}': {exc}"
        ) from exc


def _reserve_flags(overwrite: bool) -> int:
    """Flags de abertura da reserva: no-clobber (`O_EXCL`) ou `O_TRUNC`."""
    base_flags = os.O_CREAT | os.O_WRONLY
    return base_flags | os.O_TRUNC if overwrite else base_flags | os.O_EXCL


def export_path(
    dest_dir: str | None,
    source_id: str,
    base_name: str,
    *,
    ext: str | None = None,
    content_type: str | None = None,
    now: datetime | None = None,
    overwrite: bool = False,
) -> str:
    """Monta e RESERTA atomicamente o path `<UTC-ts>_<source>_<nome><ext>`.

    AC1/AC2/AC3 (DESIGN 10): a reserva usa `O_CREAT|O_EXCL` por candidato,
    então threads e reexecuções nunca perdem artefato — nome em uso gera
    sufixo `-N` antes da extensão. `overwrite=True` é a opt-in explícita de
    reescrita do MESMO arquivo (sem sufixo). A criação do diretório é
    garantida aqui (todas as tools de export passam por este helper).

    Args:
        dest_dir: pasta de destino (None/vazio = `config.EXPORTS_DIR`).
        source_id: fonte IPED, sanitizada e composta no nome (AC3).
        base_name: nome-base do artefato (com ou sem extensão; é sanitizado).
        ext: extensão forçada com ponto (ex.: `.json`); None resolve por
            `content_type` e, na falta de mapa, pela extensão de `base_name`.
        content_type: Content-Type real da resposta (P1-3), se houver.
        now: timestamp da exportação (None = `utc_now()`); o MESMO valor deve
            irrigar os metadados internos do artefato.
        overwrite: True reescreve o mesmo arquivo; default False (no-clobber).

    Returns:
        Caminho absoluto do arquivo vazio recém-reservado.

    Raises:
        ValueError: diretório não criação ou falha de sistema na reserva.
    """
    target = resolve_export_dir(dest_dir)
    _ensure_export_dir(target)
    stem, name_ext = os.path.splitext(os.path.basename(str(base_name)))
    if ext is None:
        ext = extension_for_content_type(content_type) or name_ext
    moment = now or utc_now()
    prefix = (
        f"{moment.strftime(EXPORT_TIMESTAMP_FMT)}"
        f"_{safe_name(source_id)}_{safe_name(stem)}"
    )
    attempt = 0
    while True:
        suffix = "" if attempt == 0 else f"-{attempt}"
        candidate = os.path.join(target, f"{prefix}{suffix}{ext}")
        try:
            fd = os.open(candidate, _reserve_flags(overwrite))
        except FileExistsError:
            attempt += 1
            continue
        except OSError as exc:
            raise ValueError(f"Falha ao reservar o export '{candidate}': {exc}") from exc
        os.close(fd)
        return os.path.abspath(candidate)


def write_stream_to(path: str, resp: requests.Response) -> int:
    """Escreve o stream da resposta em `path` (único `open("wb")` de export).

    Args:
        path: destino reservado por `export_path`.
        resp: resposta em streaming (iter_content).

    Returns:
        Bytes gravados (0 = corpo vazio; o chamador decide o descarte).
    """
    size = 0
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                f.write(chunk)
                size += len(chunk)
    return size


def write_bytes_to(path: str, data: bytes) -> int:
    """Grava bytes já em memória (thumbnail etc.) no destino reservado.

    Args:
        path: destino reservado por `export_path`.
        data: conteúdo completo.

    Returns:
        Bytes gravados.
    """
    with open(path, "wb") as f:
        f.write(data)
    return len(data)
