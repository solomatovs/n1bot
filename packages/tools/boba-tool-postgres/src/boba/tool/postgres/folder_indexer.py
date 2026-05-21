"""Folder → KB-коллекция индексатор (multi-format).

`FolderIndexer` walks `folder.rglob('*')` (отсортированно), для каждого
файла подбирает `ChunkParser` по расширению, делает idempotent upsert
в `PostgresVectorStore`:

- `chunk_id` — детерминирован от `source_id` + `chunk_index`. При
  повторной индексации того же файла — тот же chunk_id, upsert заменяет.
- `content_hash` — sha256 от `format_content`. Используется для
  skip-if-unchanged: перед upsert'ом читаем существующие чанки по id,
  сравниваем content_hash, пропускаем неизменённые.
- Stale-cleanup: если ранее индексированный файл удалён из папки,
  все его чанки остаются в коллекции. Включается опционально через
  `prune_missing=True` — компарируется `source_id` чанка с множеством
  source_id'ов всех обнаруженных файлов (по всем парсерам сразу, чтобы
  один формат не «съел» чанки другого).

Замечание про backend-agnostic: класс типизирован на `PostgresVectorStore`
ради DI/type-checker'а, но работает через `boba.indexing` ABC. Когда
folder_indexer/md_chunk/html_chunk вынесутся в общий пакет, тип
релаксируется до Protocol'а.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from boba.indexing.chunks import Chunk, ChunkId
from boba.indexing.context import CollectionId
from boba.indexing.index_views import TrackingKeys
from boba.indexing.sections import SourceId
from boba.tool.postgres.chunk_parser import ChunkParser
from boba.tool.postgres.vector_store import PostgresVectorStore

__all__ = [
    "FailedFile",
    "FolderIndexer",
    "IngestStats",
]

logger = logging.getLogger(__name__)

# Документирующий импорт: системные wire-ключи `source_id`, `chunk_index`,
# `content_hash` живут в отдельных колонках kb_chunks (см. 001_init.sql) —
# их пишет `PostgresVectorStore.upsert`, читает `_row_to_chunk`.
_ = TrackingKeys


@dataclass(frozen=True)
class FailedFile:
    """Один файл, который не удалось распарсить/индексировать."""

    path: str
    error: str


@dataclass(frozen=True)
class IngestStats:
    """Итоги одного запуска `FolderIndexer.index`."""

    folder: str
    collection: str
    indexed: int
    skipped_unchanged: int
    pruned: int
    failed: tuple[FailedFile, ...] = field(default_factory=tuple)


class FolderIndexer:
    """Multi-format индексатор папки в KB-коллекцию.

    Конструктор связывает `store` и список `parsers` — оба переиспользуемы
    между запусками. Метод `index(...)` принимает per-run параметры
    (folder, collection, флаги).

    Диспатч по расширениям: при walk'е файлы группируются по их
    `suffix.lower()` и направляются к парсеру, который объявил это
    расширение в `ChunkParser.extensions`. Файлы без подходящего парсера
    игнорируются молча (это значит, что у оператора в папке могут лежать
    `.gitkeep`, `.json` и т.п. — мы их не трогаем).
    """

    _PEEK_LIMIT: ClassVar[int] = 100_000
    """Хард-лимит для `peek` в prune-cleanup'е."""

    def __init__(
        self,
        store: PostgresVectorStore,
        *,
        parsers: Sequence[ChunkParser],
    ) -> None:
        if not parsers:
            msg = "FolderIndexer: at least one ChunkParser required"
            raise ValueError(msg)

        self._store = store
        # Расширение → парсер; первая регистрация выигрывает, дубли
        # игнорируются (логируем для прозрачности).
        self._dispatch: dict[str, ChunkParser] = {}
        for p in parsers:
            for ext in p.extensions:
                ext_lower = ext.lower()
                existing = self._dispatch.get(ext_lower)
                if existing is not None:
                    logger.warning(
                        "extension %s already handled by %s; "
                        "ignoring %s",
                        ext_lower,
                        type(existing).__name__,
                        type(p).__name__,
                    )
                    continue
                self._dispatch[ext_lower] = p

    def index(
        self,
        folder: Path,
        collection: CollectionId,
        *,
        collection_description: str | None = None,
        prune_missing: bool = False,
    ) -> IngestStats:
        """Проиндексировать все понятные файлы под `folder` в `collection`."""
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
        files = sorted(p for p in folder.rglob("*") if p.is_file())
        chunks: list[Chunk[str]] = []
        failed: list[FailedFile] = []
        for f in files:
            parser = self._dispatch.get(f.suffix.lower())
            if parser is None:
                continue
            try:
                file_chunks = list(
                    parser.build_chunks_from_file(f, root=folder),
                )
            except Exception as e:
                failed.append(
                    FailedFile(
                        path=str(f),
                        error=f"{type(e).__name__}: {e}",
                    ),
                )
                logger.warning("failed to parse %s: %s", f, e)
                continue
            chunks.extend(file_chunks)
        return chunks, failed

    def _partition_for_upsert(
        self,
        chunks: list[Chunk[str]],
        collection: CollectionId,
    ) -> tuple[list[Chunk[str]], int]:
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
