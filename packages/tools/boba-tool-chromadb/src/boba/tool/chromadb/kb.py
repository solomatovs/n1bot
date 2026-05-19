"""Read-only адаптер ChromaDB для KB-tools (v2: без глобальных кешей).

Process-singleton PersistentClient'а раньше жил здесь как module-level
`_CLIENT_CACHE`; теперь lifecycle управляет Dishka (`Scope.APP` +
generator-provider с teardown). Один контейнер на агент = один клиент
на persist_path, всё корректно закрывается на `Agent.close()`.
"""

from __future__ import annotations

import logging
from typing import Any

from boba.tool.chromadb.errors import (
    CollectionNotFoundError,
    KnowledgeBaseError,
)
from boba.tool.chromadb.models import CollectionInfo, SearchHit

logger = logging.getLogger(__name__)

__all__ = ["ChromaKnowledgeBase"]


class ChromaKnowledgeBase:
    """Read-only обёртка над PersistentClient."""

    def __init__(
        self,
        client: Any,
        snippet_chars: int,
        *,
        embedding_function: Any = None,
    ) -> None:
        self._client = client
        self._snippet_chars = snippet_chars
        self._embedding_function = embedding_function
        logger.info(
            "ChromaKnowledgeBase opened ef=%s",
            type(embedding_function).__name__ if embedding_function else "default",
        )

    @property
    def client(self) -> Any:
        """PersistentClient, инкапсулированный этим KB."""
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
        collection: str,
        query: str,
        top_k: int,
    ) -> list[SearchHit]:
        col = self._get_collection(collection)
        try:
            raw = col.query(query_texts=[query], n_results=top_k)
        except Exception as e:
            raise KnowledgeBaseError(
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

    def _get_collection(self, name: str) -> Any:
        try:
            return self._client.get_collection(
                name=name,
                embedding_function=self._embedding_function,
            )
        except Exception as e:
            if "does not exist" in str(e) or "not found" in str(e).lower():
                raise CollectionNotFoundError(name) from e
            raise KnowledgeBaseError(
                f"chromadb get_collection({name!r}) failed: "
                f"{type(e).__name__}: {e}",
            ) from e


def _normalize_metadata(md: object) -> dict[str, str]:
    if not isinstance(md, dict):
        return {}
    return {str(k): str(v) for k, v in md.items()}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
