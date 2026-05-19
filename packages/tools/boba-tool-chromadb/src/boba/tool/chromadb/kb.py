"""Read-only адаптер ChromaDB для KB-tools."""

from __future__ import annotations

import logging
from typing import Any

from boba.tool.chromadb.errors import (
    CollectionNotFoundError,
    KnowledgeBaseError,
)
from boba.tool.chromadb.models import CollectionInfo, SearchHit
from boba.tools.domain import ToolId

logger = logging.getLogger(__name__)


class ChromaKnowledgeBase:
    """Read-only обёртка над PersistentClient."""

    def __init__(
        self,
        persist_path: str,
        snippet_chars: int,
        *,
        embedding_function: Any = None,
    ) -> None:
        self._snippet_chars = snippet_chars
        self._client = get_chroma_client(persist_path)
        self._embedding_function = embedding_function
        logger.info(
            "ChromaKnowledgeBase opened persist_path=%r ef=%s",
            persist_path,
            type(embedding_function).__name__ if embedding_function else "default",
        )

    @property
    def client(self) -> Any:
        """PersistentClient, инкапсулированный этим KB.

        Расшариваем для write-side tool'ов (kb_ingest): один процесс на
        один persist_path должен держать единственный chromadb-клиент,
        иначе возможна contention за file-lock SQLite-бэкэнда.
        """
        return self._client

    def list_collections(self) -> list[CollectionInfo]:
        result: list[CollectionInfo] = []
        for c in self._client.list_collections():
            metadata = c.metadata or {}
            description = ""
            raw = metadata.get("description")
            if isinstance(raw, str):
                description = raw
            result.append(CollectionInfo(name=c.name, description=description))
        return result

    def search(
        self,
        tool_id: ToolId,
        collection: str,
        query: str,
        top_k: int,
    ) -> list[SearchHit]:
        col = self._get_collection(tool_id, collection)
        try:
            raw = col.query(query_texts=[query], n_results=top_k)
        except Exception as e:
            raise KnowledgeBaseError(
                tool_id,
                f"chromadb query failed for collection {collection!r}: "
                f"{type(e).__name__}: {e}",
            ) from e

        ids = (raw.get("ids") or [[]])[0]
        documents = (raw.get("documents") or [[]])[0]
        metadatas = (raw.get("metadatas") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]

        hits: list[SearchHit] = []
        for i, doc_id in enumerate(ids):
            doc = documents[i] if i < len(documents) else ""
            md = metadatas[i] if i < len(metadatas) else None
            dist = distances[i] if i < len(distances) else 0.0
            hits.append(
                SearchHit(
                    id=str(doc_id),
                    distance=float(dist),
                    metadata=_normalize_metadata(md),
                    snippet=_truncate(doc or "", self._snippet_chars),
                )
            )
        return hits

    def _get_collection(self, tool_id: ToolId, name: str):
        try:
            return self._client.get_collection(
                name=name,
                embedding_function=self._embedding_function,
            )
        except Exception as e:
            if "does not exist" in str(e) or "not found" in str(e).lower():
                raise CollectionNotFoundError(tool_id, name) from e
            raise KnowledgeBaseError(
                tool_id,
                f"chromadb get_collection({name!r}) failed: {type(e).__name__}: {e}",
            ) from e


def _normalize_metadata(md: object) -> dict[str, str]:
    if not isinstance(md, dict):
        return {}
    return {str(k): str(v) for k, v in md.items()}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


# Process-singleton chromadb.PersistentClient по persist_path: один клиент
# на путь во всём процессе. Иначе SQLite-бэкэнд chromadb словит file-lock
# contention при двух параллельных клиентах. И read-side (kb_search,
# kb_list_collections), и write-side (kb_ingest) должны проходить через
# этот кеш.
_CLIENT_CACHE: dict[str, Any] = {}

# Process-singleton ChromaKnowledgeBase по (persist_path, snippet_chars, ef-id).
_KB_CACHE: dict[tuple[str, int, int], ChromaKnowledgeBase] = {}


def get_chroma_client(persist_path: str) -> Any:
    """Process-singleton `chromadb.PersistentClient` по `persist_path`.

    Шарится между read-side `ChromaKnowledgeBase` и write-side
    `KbIngestTool`. Ленивый импорт chromadb — модуль `kb.py` грузится
    без runtime-deps.
    """
    client = _CLIENT_CACHE.get(persist_path)
    if client is None:
        import chromadb  # noqa: PLC0415

        client = chromadb.PersistentClient(path=persist_path)
        _CLIENT_CACHE[persist_path] = client
        logger.info("chromadb.PersistentClient opened persist_path=%r", persist_path)
    return client


def get_knowledge_base(
    persist_path: str,
    snippet_chars: int,
    *,
    embedding_function: Any = None,
) -> ChromaKnowledgeBase:
    """Process-singleton ChromaKnowledgeBase по (persist_path, snippet_chars, ef)."""
    key = (persist_path, snippet_chars, id(embedding_function))
    kb = _KB_CACHE.get(key)
    if kb is None:
        kb = ChromaKnowledgeBase(
            persist_path, snippet_chars, embedding_function=embedding_function,
        )
        _KB_CACHE[key] = kb
    return kb
