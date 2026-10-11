"""Tools de acesso genérico à API do IPED.

Expostas ao agente com o prefixo `iped_` (nome do servidor no opencode);
no código, os nomes não levam o prefixo.

Duas fases:
- Exploratória (JSON pequeno): list_sources, search, doc_info.
- Conteúdo (caro, explícito): doc_text (truncado), doc_export e
  thumb_export (binário para disco, nunca em contexto).
"""

from __future__ import annotations

import os
import time
from typing import Any

import requests
from fastmcp import FastMCP

from iped_mcp import config
from iped_mcp.client import TEXT_STATUS_NO_TEXT, TEXT_STATUS_OK, TextResult
from iped_mcp.diagnose import guarded_call
from iped_mcp.download import (
    export_path,
    pick_export_name,
    write_bytes_to,
    write_stream_to,
)
from iped_mcp.fetch import text_cache_get, text_cache_set
from iped_mcp.router import get_router

# Busca genérica pode ser match-all (~66 s no source de teste); timeout padrão
# (10 s) não basta. Mesma ordem de grandeza de _MATCH_ALL_TIMEOUT em regex/scan.py.
_SEARCH_TIMEOUT = 120.0

_SEARCH_HINT = (
    "Revise a sintaxe Lucene (docstring de iped_search) e os campos de extra_filters."
)
_DOC_HINT = (
    "Doc/source inexistente retorna HTTP 500; confira source_id e doc_id "
    "(iped_list_sources / iped_search)."
)

# Conjunto curado de propriedades devolvido por doc_info por padrão.
KEY_FIELDS = [
    "name",
    "ext",
    "size",
    "contentType",
    "path",
    "category",
    "created",
    "modified",
    "accessed",
    "hash",
    "ufed:MD5",
    "ufed:SHA256",
]


def _first(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


# Tradução de erros: política única em iped_mcp.diagnose.guarded_call (M-2).


def _prepare_dest_dir(dest_dir: str | None) -> tuple[str, bool]:
    """Valida/cria dest_dir → (caminho absoluto, era absoluto?).

    M-10 (DESIGN 10): `dest_dir` omitido/vazio resolve contra
    `config.EXPORTS_DIR` (`IPED_MCP_EXPORTS_DIR`, fonte única de todas as
    tools de export); o default do operador é tratado como absoluto (não
    há warning de cwd para configuração de deployment).
    """
    if not dest_dir:
        return os.path.abspath(os.path.expanduser(config.EXPORTS_DIR)), True
    is_abs = os.path.isabs(dest_dir)
    target = os.path.abspath(os.path.expanduser(dest_dir))
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        raise ValueError(
            f"Não foi possível criar o diretório de destino '{target}': {e}"
        ) from e
    return target, is_abs


def _no_file_payload(source_id: str, doc_id: int, note: str) -> dict[str, Any]:
    """Payload comum das variantes de 'doc sem arquivo' em `doc_export`."""
    return {"source": source_id, "doc_id": doc_id, "no_file": True, "note": note}


def _doc_name(source_id: str, doc_id: int) -> str | None:
    """Prop `name` do doc (fallback de filename); None se não conseguir ler."""
    try:
        props = get_router().get_document(source_id, doc_id).get("properties", {})
        return _first(props.get("name"))
    except (requests.RequestException, ValueError):
        return None


def _relative_warning(result: dict[str, Any], is_abs: bool, dest_dir: str) -> dict[str, Any]:
    if not is_abs:
        result["warning"] = (
            f"dest_dir '{dest_dir}' é relativo e resolve contra o cwd do "
            "servidor (não do seu projeto). Use um caminho absoluto."
        )
    return result


# TTL curto da listagem: re-sonda os N endpoints a cada chamada; o cache evita
# o custo de chamadas repetidas do agente. O refresh interno do router
# (source novo em runtime) não passa por aqui, então continua funcionando.
_SOURCES_TTL = 30.0
_sources_cache: tuple[float, list[dict[str, Any]]] | None = None


def list_sources() -> dict[str, Any]:
    """Lista os sources registrados no servidor IPED e reporta servidores mudos.

    Comece por aqui para ver os sources disponíveis antes de buscar.
    Cada source tem `id` (use em source_id das outras tools) e `path`.
    Servidor que não respondeu NÃO aparece em `sources` (não há como listar
    suas fontes); a falha aparece em `offline_endpoints` como dado — uma
    lista vazia com servidores offline nunca é silenciosa.

    Returns:
        {"sources": [{id, path}, ...],
         "offline_endpoints": [{"endpoint": id, "error": motivo}, ...]} —
         `offline_endpoints` vazio quando todos os servidores responderam.

    Raises:
        ValueError: falha inesperada de comunicação (taxonomia M-2).
    """
    global _sources_cache
    now = time.monotonic()
    if _sources_cache is not None and now - _sources_cache[0] < _SOURCES_TTL:
        return _sources_cache[1]
    sources, failures = get_router().list_sources_status()
    result: dict[str, Any] = {
        "sources": sources,
        "offline_endpoints": [
            {"endpoint": eid, "error": reason}
            for eid, reason in sorted(failures.items())
        ],
    }
    _sources_cache = (now, result)
    return result


def search(
    query: str,
    source_id: str,
    limit: int = 50,
    extra_filters: list[str] | None = None,
) -> dict[str, Any]:
    """Busca Lucene livre no IPED. Retorna contagem total + amostra de doc IDs (nunca a lista cheia).

    Use os IDs retornados com iped_doc_info (propriedades), iped_doc_text
    (texto), iped_doc_export (arquivo bruto) ou iped_thumb_export (thumbnail).

    SINTAXE DE BUSCA LIVRE (query) — case-insensitive:
    - palavra: casa a palavra exata; NÃO casa variantes/plural (suspeito ≠ suspeitos).
    - palavra?: ? = 1 caractere qualquer (suspeit? → suspeito, suspeita).
    - palavra*: * = prefixo (suspei* → suspeito, suspeitas, suspeição).
    - palavra~: fuzzy, palavras parecidas (propina~ → propina, propinas); lento em digitalizações.
    - "frase": expressão exata, ordem fixa ("polícia civil"); * ? ~ não funcionam dentro.
    - "frase"~N: frase com até N palavras de distância (slop).
    - a b: OR implícito (qualquer das palavras).
    - a AND b: ambas as palavras.
    - a -b: contém a, não contém b.
    - (a b) && c: c E (a OU b).
    - (a b c)@N: pelo menos N palavras do conjunto.

    EXTRA_FILTERS — filtros de campo combinados com AND (sintaxe campo:valor):
    - ext:txt, ext:png, ext:jpg, ext:pdf (extensão)
    - contentType:text/plain (tipo MIME)
    - nome:foo / name:foo (nome do arquivo; wildcard ok: nome:backup*)
    - tamanho:[5000 TO *] / size:… (faixa de bytes; {} = intervalo aberto)
    - modificacao:[2025-08-01 TO 2025-08-31T23:59:59Z] / modified:… (data)
    - created:… (data de criação)
    - path:… (caminho)
    Campo inexistente → 0 resultados (sem erro).

    REFINANDO:
    - Muitos resultados → restrinja: extra_filters com ext:/tamanho:/modificacao:, ou AND com palavras mais específicas.
    - Poucos resultados → amplie: ~ (fuzzy), * (prefixo), ? (1 caractere), ou remova AND.
    - Expressão exata → "frase"; tolerante a ordem/distância → "frase"~N.
    - Excluir ruído → -palavra.

    RETORNO:
    - total: nº total de docs (pode ser milhões — não é a lista).
    - by_source: contagem por source.
    - ids: primeiros `limit` IDs (amostra).
    - truncated: true se total > limit → refine a query, nunca peça a lista cheia.

    Args:
        query: query Lucene livre (palavras, "frase", *, ~, AND, etc.).
        source_id: source no IPED (obrigatório; veja iped_list_sources).
        limit: máximo de IDs a devolver (padrão 50, teto 500).
        extra_filters: filtros de campo combinados com AND (ex.: ["ext:txt", "tamanho:[5000 TO *]"]).
    """
    if not source_id or not source_id.strip():
        raise ValueError(
            "'source_id' é obrigatório. Use iped_list_sources para ver os "
            "sources disponíveis."
        )
    if not query or not query.strip():
        raise ValueError("'query' não pode ser vazio.")
    if not 1 <= limit <= 500:
        raise ValueError("limit deve estar entre 1 e 500.")
    parts = [query.strip()]
    if extra_filters:
        parts.extend(f.strip() for f in extra_filters if f and f.strip())
    final_query = " AND ".join(parts)
    client = get_router()
    results = guarded_call(
        client.search,
        f"GET /search?q={final_query!r}",
        _SEARCH_HINT,
        final_query,
        source_id=source_id,
        timeout=_SEARCH_TIMEOUT,
    )
    by_source: dict[str, int] = {}
    all_ids: list[int] = []
    for group in results:
        src = group.get("source")
        ids = group.get("ids", [])
        by_source[src] = by_source.get(src, 0) + len(ids)
        all_ids.extend(ids)
    total = len(all_ids)
    result: dict[str, Any] = {
        "total": total,
        "by_source": by_source,
        "ids": all_ids[:limit],
        "truncated": total > limit,
    }
    return result


def doc_info(
    source_id: str,
    doc_id: int,
    fields: list[str] | None = None,
    all: bool = False,
) -> dict[str, Any]:
    """Propriedades de um documento do source (fase exploratória, JSON pequeno).

    Por padrão devolve um conjunto curado de campos-chave (name, ext, size,
    contentType, path, category, created, modified, accessed, hash, ufed:MD5,
    ufed:SHA256) + bookmarks e selected. Use `fields` para só alguns campos ou
    `all=true` para o properties completo (pode ser grande).

    Args:
        source_id: source no IPED (veja iped_list_sources).
        doc_id: id do documento (ex.: vindo de iped_search).
        fields: propriedades específicas a devolver (ausentes não aparecem).
        all: true → properties completo (padrão false).
    """
    client = get_router()
    doc = guarded_call(
        client.get_document,
        f"GET /sources/{source_id}/docs/{doc_id}",
        _DOC_HINT,
        source_id,
        doc_id,
    )
    props = doc.get("properties", {})
    if all:
        selected = props
    elif fields:
        wanted = set(fields)
        selected = {k: v for k, v in props.items() if k in wanted}
    else:
        selected = {k: props[k] for k in KEY_FIELDS if k in props}
    return {
        "source": doc.get("source"),
        "id": doc.get("id"),
        "bookmarks": doc.get("bookmarks", []),
        "selected": doc.get("selected", False),
        "properties": selected,
    }


def doc_text(
    source_id: str,
    doc_id: int,
    max_chars: int = 20000,
    offset: int = 0,
) -> dict[str, Any]:
    """Texto extraído de um documento, truncado e paginado (fase de conteúdo).

    O endpoint /text pode devolver texto gigante (centenas de KB num doc só),
    então a tool devolve no máximo `max_chars` caracteres a partir de `offset`.
    Use `next_offset` do retorno para ler o próximo pedaço.

    Estados (DESIGN 08 / M-8 — status como dado, nunca exceção):
    `status="ok"` com `text`; ou `status="no_text"` (+ `no_text=true`)
    quando o IPED não devolve texto (HTTP 500 do fork — doc binário: use
    iped_doc_export) ou devolve corpo vazio. `capped=true` marca texto
    truncado no teto de leitura do cliente (`IPED_MCP_TEXT_CAP`, 10 MB).

    Args:
        source_id: source no IPED (veja iped_list_sources).
        doc_id: id do documento (ex.: vindo de iped_search).
        max_chars: máximo de caracteres a devolver (padrão 20000, teto 100000).
        offset: caractere inicial (paginação; padrão 0).
    """
    if not 1 <= max_chars <= 100000:
        raise ValueError("max_chars deve estar entre 1 e 100000.")
    if offset < 0:
        raise ValueError("offset deve ser >= 0.")
    read = text_cache_get(source_id, doc_id)
    if read is None:
        # guarded_call continua traduzindo falhas reais (rede, 404, rede
        # viva com erro); o 500 de "sem texto" já chega como estado
        # no_text do cliente e o cache também o armazena (barato re-usar).
        client = get_router()
        read = guarded_call(
            client.get_document_text,
            f"GET /sources/{source_id}/docs/{doc_id}/text",
            _DOC_HINT,
            source_id,
            doc_id,
        )
        text_cache_set(source_id, doc_id, read)
    if read.status == TEXT_STATUS_NO_TEXT:
        return _doc_text_no_text(
            source_id,
            doc_id,
            "doc sem texto extraível (o IPED não devolve texto p/ este doc); "
            "use iped_doc_export para o arquivo bruto.",
        )
    return _doc_text_segment(read, source_id, doc_id, max_chars, offset)


def _doc_text_segment(
    read: TextResult, source_id: str, doc_id: int, max_chars: int, offset: int
) -> dict[str, Any]:
    """Monta o payload da janela [offset, offset+max_chars) de uma leitura ok.

    Args:
        read: ``TextResult`` do cliente (status ok; no_text tratado antes).
        source_id: source no IPED.
        doc_id: id do documento.
        max_chars: máximo de caracteres da janela.
        offset: caractere inicial da janela.

    Returns:
        Payload `status ok` com paginação; `no_text` quando o corpo é vazio
        (HTTP 200 sem texto) ou o offset está além do fim — com capped sempre
        presente (DESIGN 08 / M-8).
    """
    if not read.text.strip():
        return _doc_text_no_text(
            source_id,
            doc_id,
            "texto extraído vazio (HTTP 200 sem corpo); "
            "use iped_doc_export para o arquivo bruto.",
        )
    segment = read.text[offset : offset + max_chars]
    if not segment:
        return _doc_text_no_text(
            source_id,
            doc_id,
            f"offset {offset} além do fim do texto (total_chars={read.total_chars}).",
        )
    truncated = (offset + max_chars) < read.total_chars
    result: dict[str, Any] = {
        "source": source_id,
        "doc_id": doc_id,
        "status": TEXT_STATUS_OK,
        "text": segment,
        "total_chars": read.total_chars,
        "offset": offset,
        "truncated": truncated,
        "next_offset": (offset + max_chars) if truncated else None,
        "capped": read.capped,
    }
    if read.capped:
        result["note"] = (
            f"texto truncado no teto de leitura ({read.total_bytes} bytes "
            "lidos, IPED_MCP_TEXT_CAP); total_chars é piso."
        )
    return result


def _doc_text_no_text(source_id: str, doc_id: int, note: str) -> dict[str, Any]:
    """Retorno canônico do estado no_text (DESIGN 08 / M-8 — status como dado).

    Args:
        source_id: source no IPED.
        doc_id: id do documento.
        note: explicação do sub-estado (sem texto no servidor, corpo vazio
            ou offset além do fim) — os três preservam status no_text.

    Returns:
        Payload com status no_text, marcador legado no_text=true e capped=False.
    """
    return {
        "source": source_id,
        "doc_id": doc_id,
        "status": TEXT_STATUS_NO_TEXT,
        "no_text": True,
        "capped": False,
        "note": note,
    }


def doc_export(
    source_id: str,
    doc_id: int,
    dest_dir: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Salva o arquivo bruto de um documento (binário nunca entra em contexto).

    O conteúdo é baixado em streaming para o disco; a tool devolve o path do
    arquivo salvo (não os bytes). Nome do artefato (M-10):
    `<UTC-ts>_<source>_<nome>` — uma reexecução NUNCA sobrescreve o export
    anterior (colisão ganha sufixo `-N`; `overwrite=True` é a opt-in
    explícita). A extensão segue o Content-Type real (P1-3) quando há mapa;
    sem mapa vale a do nome. Filename do header Content-Disposition →
    fallback prop name → doc_{id}. Doc sem arquivo → no_file=true.

    Args:
        source_id: source no IPED (veja iped_list_sources).
        doc_id: id do documento (ex.: vindo de iped_search).
        dest_dir: pasta de destino (recomendado: caminho absoluto; criada se
            não existir; omissão usa `$IPED_MCP_EXPORTS_DIR`).
        overwrite: True permite reescrever o MESMO arquivo na reexecução
            (default False: artefato anterior é intocável — AC1/M-10).
    """
    target, is_abs = _prepare_dest_dir(dest_dir)
    client = get_router()
    header_filename, resp = guarded_call(
        client.get_document_content,
        f"GET /sources/{source_id}/docs/{doc_id}/content",
        _DOC_HINT,
        source_id,
        doc_id,
    )
    try:
        with resp:
            if resp.headers.get("Content-Length") == "0":
                return _relative_warning(
                    _no_file_payload(
                        source_id, doc_id, "doc sem arquivo (ex.: Device Information)."
                    ),
                    is_abs,
                    dest_dir,
                )
            doc_name = _doc_name(source_id, doc_id) if not header_filename else None
            base = pick_export_name(doc_id, header_filename, doc_name)
            path = export_path(
                target, source_id, base,
                content_type=resp.headers.get("Content-Type"),
                overwrite=overwrite,
            )
            size = write_stream_to(path, resp)
            if size == 0:
                os.remove(path)
                return _relative_warning(
                    _no_file_payload(source_id, doc_id, "doc sem arquivo (body vazio)."),
                    is_abs,
                    dest_dir,
                )
            return _relative_warning(
                {
                    "source": source_id,
                    "doc_id": doc_id,
                    "path": path,
                    "filename": os.path.basename(path),
                    "size": size,
                },
                is_abs,
                dest_dir,
            )
    except requests.RequestException as exc:
        raise ValueError(
            f"Falha ao baixar o conteúdo do doc {doc_id} de {source_id}: {exc}"
        ) from exc


def thumb_export(
    source_id: str,
    doc_id: int,
    dest_dir: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Salva a thumbnail (JPEG 96x72) de um documento (binário nunca entra em contexto).

    A thumbnail é baixada para o disco; a tool devolve o path do arquivo
    salvo. Nome (M-10): `<UTC-ts>_<source>_<name>_thumb.jpg`
    (fallback doc_{id}_thumb.jpg); reexecução nunca sobrescreve (colisão
    → `-N`; `overwrite=True` é a opt-in explícita). Doc sem imagem →
    no_thumb=true.

    Args:
        source_id: source no IPED (veja iped_list_sources).
        doc_id: id do documento (ex.: vindo de iped_search).
        dest_dir: pasta de destino (recomendado: caminho absoluto; criada se
            não existir; omissão usa `$IPED_MCP_EXPORTS_DIR`).
        overwrite: True permite reescrever o MESMO arquivo na reexecução.
    """
    target, is_abs = _prepare_dest_dir(dest_dir)
    client = get_router()
    resp = guarded_call(
        client.get_document_thumb,
        f"GET /sources/{source_id}/docs/{doc_id}/thumb",
        _DOC_HINT,
        source_id,
        doc_id,
    )
    try:
        with resp:
            data = resp.content
            if not data:
                return _relative_warning(
                    {
                        "source": source_id,
                        "doc_id": doc_id,
                        "no_thumb": True,
                        "note": "doc sem imagem (thumbnail vazia).",
                    },
                    is_abs,
                    dest_dir,
                )
            name = _doc_name(source_id, doc_id)
            base = os.path.basename(str(name)) if name else f"doc_{doc_id}"
            path = export_path(
                target, source_id, f"{base}_thumb", ext=".jpg", overwrite=overwrite
            )
            write_bytes_to(path, data)
            return _relative_warning(
                {
                    "source": source_id,
                    "doc_id": doc_id,
                    "path": path,
                    "filename": os.path.basename(path),
                    "size": len(data),
                },
                is_abs,
                dest_dir,
            )
    except requests.RequestException as exc:
        raise ValueError(
            f"Falha ao baixar a thumbnail do doc {doc_id} de {source_id}: {exc}"
        ) from exc


def register(mcp: FastMCP) -> None:
    mcp.add_tool(list_sources)
    mcp.add_tool(search)
    mcp.add_tool(doc_info)
    mcp.add_tool(doc_text)
    mcp.add_tool(doc_export)
    mcp.add_tool(thumb_export)
