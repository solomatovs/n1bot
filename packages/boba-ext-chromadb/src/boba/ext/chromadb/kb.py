"""Адаптер ChromaDB."""

from __future__ import annotations

import logging

from boba.domain.core.tools import ToolId
from boba.ext.chromadb.config import ChromaExtConfig
from boba.ext.chromadb.errors import CollectionNotFoundError, KnowledgeBaseError
from boba.ext.chromadb.models import CollectionInfo, SearchHit

logger = logging.getLogger(__name__)


class ChromaKnowledgeBase:
    """Read-only обёртка над PersistentClient."""

    def __init__(self, cfg: ChromaExtConfig) -> None:
        # Ленивый импорт chromadb — чтобы модуль импортировался без runtime-deps.
        import chromadb  # noqa: PLC0415

        self._cfg = cfg
        self._client = chromadb.PersistentClient(path=cfg.persist_path)
        logger.info(
            "ChromaKnowledgeBase opened persist_path=%r", cfg.persist_path
        )

    def list_collections(self) -> list[CollectionInfo]:
        """Все коллекции; description берётся из metadata["description"]."""
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
        """Semantic search по одной коллекции; до top_k hits по возрастанию distance."""
        col = self._get_collection(tool_id, collection)
        try:
            raw = col.query(query_texts=[query], n_results=top_k)
        except Exception as e:
            raise KnowledgeBaseError(
                tool_id,
                f"chromadb query failed for collection {collection!r}: "
                f"{type(e).__name__}: {e}",
            ) from e

        # chromadb возвращает список списков (по одному на query_text); у нас один query.
        ids = (raw.get("ids") or [[]])[0]
        documents = (raw.get("documents") or [[]])[0]
        metadatas = (raw.get("metadatas") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]

        snippet_chars = self._cfg.snippet_chars
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
                    snippet=_truncate(doc or "", snippet_chars),
                )
            )
        return hits

    def _get_collection(self, tool_id: ToolId, name: str):
        try:
            return self._client.get_collection(name=name)
        except Exception as e:
            # Любая «не нашли» от chromadb → CollectionNotFoundError.
            if "does not exist" in str(e) or "not found" in str(e).lower():
                raise CollectionNotFoundError(tool_id, name) from e
            raise KnowledgeBaseError(
                tool_id,
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


# --- Singleton-фабрика ---------------------------------------------------
# Один Chroma persistent-клиент на процесс (SQLite + warm embedding-cache).

_KB_CACHE: dict[int, ChromaKnowledgeBase] = {}


def get_knowledge_base(cfg: ChromaExtConfig) -> ChromaKnowledgeBase:
    """Process-singleton ChromaKnowledgeBase под ключом id(cfg)."""
    key = id(cfg)
    kb = _KB_CACHE.get(key)
    if kb is None:
        kb = ChromaKnowledgeBase(cfg)
        _KB_CACHE[key] = kb
    return kb
