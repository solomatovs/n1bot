"""
Индексация папки `.md` файлов в ChromaDB-коллекцию.

`MdFolderIndexer` walks `folder.rglob('*.md')` (отсортированно), парсит
каждый файл через `MdChunkParser` (`MarkdownSectionParser` под капотом),
делает idempotent upsert в `ChromaVectorStore`:

- `chunk_id` — детерминирован от `source_id` (= относительный путь файла от
  корня индексируемой папки). При повторной индексации того же файла —
  тот же `chunk_id`, поэтому upsert заменяет, а не дублирует.
- `content_hash` — sha256 от `format_content`. Используется для
  skip-if-unchanged: перед upsert'ом читаем существующие чанки по id,
  сравниваем content_hash, пропускаем неизменённые.
- Stale-cleanup: если ранее индексированный файл удалён из папки, его
  чанк остаётся в коллекции. Включается опционально через
  `prune_missing=True` (находит чанки в коллекции, чьих source_id нет в
  текущем `chunks`, и удаляет их).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from boba.indexing.chunks import Chunk, ChunkId
from boba.indexing.context import CollectionId
from boba.indexing.index_views import TrackingKeys
from boba.indexing.sections import SourceId
from boba.tool.chromadb.md_chunk import MdChunkParser
from boba.tool.chromadb.vector_store import ChromaVectorStore

__all__ = [
    "FailedFile",
    "IngestStats",
    "MdFolderIndexer",
]

logger = logging.getLogger(__name__)

# `TrackingKeys` импортирован чтобы документировать: source_id, chunk_index,
# content_hash хранятся в Chroma-metadata через эти wire-имена. Их пишет
# `ChromaVectorStore._encode_metadata`, читает `_build_summary` / `_build_chunk`.
_ = TrackingKeys


@dataclass(frozen=True)
class FailedFile:
    """Один файл, который не удалось распарсить/индексировать."""

    path: str
    error: str


@dataclass(frozen=True)
class IngestStats:
    """Итоги одного запуска `MdFolderIndexer.index`."""

    folder: str
    collection: str
    indexed: int
    skipped_unchanged: int
    pruned: int
    failed: tuple[FailedFile, ...] = field(default_factory=tuple)


class MdFolderIndexer:
    """Индексатор папки `.md` файлов в ChromaDB-коллекцию.

    Конструктор связывает `store` и `parser` — оба переиспользуемы между
    запусками. Метод `index(...)` принимает per-run параметры (folder,
    collection, флаги). Это позволяет один экземпляр держать в плагине,
    а парсер/store оставить общими между ingest-вызовами.
    """

    _PEEK_LIMIT: ClassVar[int] = 100_000
    """Хард-лимит для `peek` в prune-cleanup'е.

    Для коллекций в десятки тысяч записей этого достаточно; для больших
    — нужен paginated cleanup через Filter (`NotIn(source_id, list)`).
    """

    def __init__(
        self,
        store: ChromaVectorStore,
        *,
        parser: MdChunkParser | None = None,
    ) -> None:
        self._store = store
        self._parser = parser if parser is not None else MdChunkParser()

    def index(
        self,
        folder: Path,
        collection: CollectionId,
        *,
        collection_description: str | None = None,
        prune_missing: bool = False,
    ) -> IngestStats:
        """Проиндексировать все `.md` файлы под `folder` в `collection`.

        Args:
            folder: корневая папка с `.md` чанками (рекурсивный walk).
            collection: имя ChromaDB-коллекции; создаётся при отсутствии.
            collection_description: description, прописываемое в metadata
                коллекции при её создании (видно через kb_list_collections).
            prune_missing: если True, удалить чанки коллекции, чьих
                `source_id` нет среди индексируемых файлов (full-cleanup
                семантика).
        """
        self._validate_folder(folder)
        self._store.ensure_collection(
            collection, description=collection_description,
        )

        chunks, failed = self._parse_files(folder)
        to_upsert, skipped_count = self._partition_for_upsert(
            chunks, collection,
        )
        self._upsert(collection, to_upsert, skipped=skipped_count)

        pruned_count = 0
        if prune_missing:
            pruned_count = self._prune_stale(
                collection,
                keep_source_ids={c.source_id for c in chunks},
            )

        return IngestStats(
            folder=str(folder),
            collection=str(collection),
            indexed=len(to_upsert),
            skipped_unchanged=skipped_count,
            pruned=pruned_count,
            failed=tuple(failed),
        )

    @staticmethod
    def _validate_folder(folder: Path) -> None:
        if not folder.exists():
            msg = f"folder does not exist: {folder}"
            raise FileNotFoundError(msg)
        if not folder.is_dir():
            msg = f"not a directory: {folder}"
            raise NotADirectoryError(msg)

    def _parse_files(
        self, folder: Path,
    ) -> tuple[list[Chunk[str]], list[FailedFile]]:
        files = sorted(p for p in folder.rglob("*.md") if p.is_file())
        chunks: list[Chunk[str]] = []
        failed: list[FailedFile] = []
        for f in files:
            try:
                chunks.append(
                    self._parser.build_chunk_from_file(f, root=folder),
                )
            except Exception as e:  # noqa: BLE001
                failed.append(
                    FailedFile(
                        path=str(f),
                        error=f"{type(e).__name__}: {e}",
                    ),
                )
                logger.warning("failed to parse %s: %s", f, e)
        return chunks, failed

    def _partition_for_upsert(
        self,
        chunks: list[Chunk[str]],
        collection: CollectionId,
    ) -> tuple[list[Chunk[str]], int]:
        """Разделить чанки на (to_upsert, skipped) по content_hash diff."""
        if not chunks:
            return [], 0
        existing_hashes = self._existing_content_hashes(
            collection, (c.chunk_id for c in chunks),
        )
        to_upsert: list[Chunk[str]] = []
        skipped_count = 0
        for c in chunks:
            new_hash = c.content_hash.to_wire() if c.content_hash else None
            prev_hash = existing_hashes.get(c.chunk_id)
            if prev_hash is not None and prev_hash == new_hash:
                skipped_count += 1
            else:
                to_upsert.append(c)
        return to_upsert, skipped_count

    def _existing_content_hashes(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> dict[ChunkId, str]:
        """content_hash существующих чанков по id для skip-if-unchanged."""
        out: dict[ChunkId, str] = {}
        for existing in self._store.get_by_ids(collection, chunk_ids):
            if existing.content_hash is not None:
                out[existing.chunk_id] = existing.content_hash.to_wire()
        return out

    def _upsert(
        self,
        collection: CollectionId,
        chunks: list[Chunk[str]],
        *,
        skipped: int,
    ) -> None:
        if not chunks:
            return
        self._store.upsert(collection, chunks)
        logger.info(
            "upserted %d chunks into %r (skipped %d unchanged)",
            len(chunks), str(collection), skipped,
        )

    def _prune_stale(
        self,
        collection: CollectionId,
        *,
        keep_source_ids: set[SourceId],
    ) -> int:
        """Удалить чанки коллекции, чей source_id отсутствует в keep-set."""
        stale_ids: list[ChunkId] = [
            summary.chunk_id
            for summary in self._store.peek(
                collection, source_id=None, limit=self._PEEK_LIMIT,
            )
            if summary.source_id not in keep_source_ids
        ]
        if stale_ids:
            self._store.delete(collection, stale_ids)
            logger.info(
                "pruned %d stale chunks from %r",
                len(stale_ids), str(collection),
            )
        return len(stale_ids)
