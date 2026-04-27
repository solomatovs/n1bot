"""Высокоуровневая логика индексации: file → reader → chunks → upsert."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from boba.cli.vector_index.chunking import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    split_text,
)
from boba.cli.vector_index.readers import (
    UnsupportedFormatError,
    reader_for,
)
from boba.cli.vector_index.store import VectorStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexStats:
    """Сводка по одному index-вызову."""

    files_indexed: int
    files_skipped: int
    chunks_upserted: int
    chunks_deleted: int


@dataclass(frozen=True)
class IndexOptions:
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP


def index_paths(
    store: VectorStore,
    collection_name: str,
    paths: list[str],
    *,
    description: str | None,
    options: IndexOptions,
) -> IndexStats:
    """Проиндексировать paths (файлы и/или директории) в коллекцию."""
    store.get_or_create_collection(collection_name, description)

    files_indexed = 0
    files_skipped = 0
    chunks_upserted = 0
    chunks_deleted = 0

    for file_path in _walk_files(paths):
        try:
            reader = reader_for(file_path)
        except UnsupportedFormatError as e:
            logger.warning("%s; skipped", e)
            files_skipped += 1
            continue
        try:
            file_mtime = str(int(Path(file_path).stat().st_mtime))
        except OSError as e:
            logger.warning("stat failed for %r: %s; skipped", file_path, e)
            files_skipped += 1
            continue

        for doc in reader.read(file_path):
            deleted = store.delete_by_source(collection_name, doc.source_path)
            chunks_deleted += deleted

            chunks = split_text(
                doc.text,
                chunk_size=options.chunk_size,
                chunk_overlap=options.chunk_overlap,
            )
            if not chunks:
                logger.info("empty after chunking: %r; skipped", file_path)
                continue

            payload: list[tuple[str, str, dict[str, str]]] = []
            for i, chunk in enumerate(chunks):
                metadata = {
                    **doc.metadata,
                    "source_path": doc.source_path,
                    "chunk_index": str(i),
                    "file_mtime": file_mtime,
                }
                payload.append((_chunk_id(doc.source_path, i), chunk, metadata))
            store.upsert_chunks(collection_name, payload)
            chunks_upserted += len(payload)
            logger.info(
                "indexed %r → collection %r: %d chunks (deleted %d old)",
                file_path,
                collection_name,
                len(payload),
                deleted,
            )
        files_indexed += 1

    return IndexStats(
        files_indexed=files_indexed,
        files_skipped=files_skipped,
        chunks_upserted=chunks_upserted,
        chunks_deleted=chunks_deleted,
    )


def _walk_files(paths: list[str]) -> Iterator[str]:
    """Раскрыть смесь файлов и директорий в плоский список файлов; скрытые пропускаются."""
    for raw in paths:
        p = Path(raw)
        if p.is_file():
            if not p.name.startswith("."):
                yield str(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file():
                    continue
                if any(seg.startswith(".") for seg in f.parts):
                    continue
                yield str(f)
        else:
            logger.warning("path not found: %r; skipped", raw)


def _chunk_id(source_path: str, chunk_index: int) -> str:
    """Стабильный id чанка: SHA1 от пути + индекс."""
    digest = hashlib.sha1(
        source_path.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return f"{digest[:16]}:{chunk_index}"
