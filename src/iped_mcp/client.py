"""Cliente para a API REST do IPED."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv

from iped_mcp.config import WORKERS

load_dotenv()

DEFAULT_IPED_URL = os.environ.get("IPED_URL", "http://localhost:1234")
# Timeout de leitura (entre bytes) para download de arquivos; arquivos grandes
# em conexões lentas podem levar mais que o timeout padrão.
_CONTENT_READ_TIMEOUT = 120.0
# Teto de leitura do /text (chunked, sem Content-Length): evita estourar memória
# em docs com texto extraído gigante. Acima disso, capped=True.
_TEXT_READ_CAP = 10 * 1024 * 1024  # 10 MB
# Pool de conexões casado com o paralelismo de fetch (config.WORKERS): com o
# pool padrão (10), workers > 10 descartam conexões ("pool is full") e parte
# dos requests paga um novo handshake TCP. +8 de folga p/ buscas concorrentes.
_POOL_SIZE = WORKERS + 8


class IPEDClient:
    """Cliente para a API REST do IPED."""

    def __init__(self, base_url: str = DEFAULT_IPED_URL, timeout: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def _get(self, path: str, timeout: float | None = None, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self._session.get(
            url, timeout=timeout if timeout is not None else self.timeout, **kwargs
        )
        resp.raise_for_status()
        return resp.json()

    def list_sources(self, timeout: float | None = None) -> list[dict[str, Any]]:
        """Lista os sources registrados no servidor IPED."""
        return self._get("sources/", timeout=timeout)["data"]

    def search(
        self,
        query: str,
        source_id: str | None = None,
        timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        """Busca documentos por query Lucene, opcionalmente restrita a um source."""
        params: dict[str, str] = {"q": query}
        if source_id:
            params["sourceID"] = source_id
        return self._get("search", params=params, timeout=timeout)["data"]

    def get_document(self, source_id: str, doc_id: int) -> dict[str, Any]:
        """Retorna o documento (incluindo `properties`)."""
        return self._get(f"sources/{source_id}/docs/{doc_id}")

    # --- Bookmarks (marcadores) ---
    # Tudo dá HTTP 200; mutações (POST/PUT/DELETE) retornam body vazio —
    # checar status_code, nunca resp.json(). Nomes com espaço exigem quote().

    def list_bookmarks(self) -> list[str]:
        """Lista os nomes dos bookmarks."""
        return self._get("bookmarks")["data"]

    def get_bookmark_docs(self, bookmark: str) -> list[dict[str, Any]]:
        """Docs do bookmark agrupados por source: [{"source": ..., "ids": [...]}].

        Bookmark inexistente → [] (HTTP 200).
        """
        return self._get(f"bookmarks/{quote(bookmark)}")["data"]

    def create_bookmark(self, name: str) -> None:
        """Cria um bookmark vazio (POST, body vazio). Não erroa se já existe."""
        self._session.post(
            f"{self.base_url}/bookmarks/{quote(name)}", timeout=self.timeout
        ).raise_for_status()

    def add_to_bookmark(self, bookmark: str, docs: list[dict[str, Any]]) -> None:
        """Adiciona docs ao bookmark. docs: [{"source": str, "id": int}].

        Doc inexistente é ignorado silenciosamente.
        """
        self._session.put(
            f"{self.base_url}/bookmarks/{quote(bookmark)}/add",
            json=docs,
            timeout=self.timeout,
        ).raise_for_status()

    def remove_from_bookmark(self, bookmark: str, docs: list[dict[str, Any]]) -> None:
        """Remove docs do bookmark. docs: [{"source": str, "id": int}]."""
        self._session.put(
            f"{self.base_url}/bookmarks/{quote(bookmark)}/remove",
            json=docs,
            timeout=self.timeout,
        ).raise_for_status()

    def rename_bookmark(self, old_name: str, new_name: str) -> None:
        """Renomeia um bookmark."""
        self._session.put(
            f"{self.base_url}/bookmarks/{quote(old_name)}/rename/{quote(new_name)}",
            timeout=self.timeout,
        ).raise_for_status()

    def delete_bookmark(self, name: str) -> None:
        """Exclui um bookmark (não os documentos indexados)."""
        self._session.delete(
            f"{self.base_url}/bookmarks/{quote(name)}", timeout=self.timeout
        ).raise_for_status()

    def get_document_content(
        self, source_id: str, doc_id: int
    ) -> tuple[str, requests.Response]:
        """→ (filename, response); filename do header Content-Disposition.

        O download faz streaming via response.iter_content(). Body vazio
        (Content-Length: 0) = doc sem arquivo.
        """
        resp = self._session.get(
            f"{self.base_url}/sources/{source_id}/docs/{doc_id}/content",
            timeout=(self.timeout, _CONTENT_READ_TIMEOUT),
        )
        resp.raise_for_status()
        filename = ""
        cd = resp.headers.get("Content-Disposition", "")
        if 'filename="' in cd:
            filename = cd.split('filename="', 1)[1].split('"', 1)[0]
        return filename, resp

    def get_document_text(
        self, source_id: str, doc_id: int, max_bytes: int = _TEXT_READ_CAP
    ) -> tuple[str, int, bool]:
        """→ (text, total_chars, capped). Texto extraído do doc (endpoint /text).

        O stream é chunked (sem Content-Length): lê até max_bytes para contar o
        total. capped=True → total_chars é piso (o texto real é maior).
        """
        resp = self._session.get(
            f"{self.base_url}/sources/{source_id}/docs/{doc_id}/text",
            timeout=(self.timeout, _CONTENT_READ_TIMEOUT),
        )
        resp.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        capped = False
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                capped = True
                break
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return text, len(text), capped

    def get_document_thumb(self, source_id: str, doc_id: int) -> requests.Response:
        """→ Response bruto da thumbnail (JPEG). Body vazio = doc sem imagem."""
        resp = self._session.get(
            f"{self.base_url}/sources/{source_id}/docs/{doc_id}/thumb",
            timeout=(self.timeout, _CONTENT_READ_TIMEOUT),
        )
        resp.raise_for_status()
        return resp
