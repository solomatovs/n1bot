"""Streaming index pipeline — индексация документов в векторную базу.

Реализован как Pipeline[IndexContext, IndexEvent] из 4 стадий:
    ManifestCheckStage   — проверка изменений файлов
    CollectionPrepStage  — пересоздание коллекции
    FileIndexStage       — чанкинг + сохранение в vectorstore
    ManifestSaveStage    — сохранение манифеста

    for event in run_indexing(ctx):
        match event:
            case FileStarted(...): ...
            case IndexingDone(...): ...
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Union

from langchain_core.documents import Document

from application.readers.registry import DocumentReaderRegistry
from domain.core.pipeline import Pipeline, PipelineStage
from domain.core.vectorstore import VectorStoreService

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Контекст индексации (shared state между стадиями)
# ---------------------------------------------------------------------------

@dataclass
class IndexContext:
    """Входные данные + промежуточные результаты индексации."""
    # --- Входные ---
    source_path: Path
    manifest_path: Path
    collection_name: str
    embedding_model: str
    vectorstore_service: VectorStoreService
    reader_registry: DocumentReaderRegistry

    # --- Промежуточные (заполняются стадиями) ---
    current_manifest: dict[str, str] = field(default_factory=dict)
    skipped: bool = False
    total_files: int = 0
    total_chunks: int = 0


# ---------------------------------------------------------------------------
# События
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ManifestChecked:
    files_changed: bool
    file_count: int

@dataclass(frozen=True)
class IndexingSkipped:
    collection: str
    doc_count: int

@dataclass(frozen=True)
class CollectionPrepared:
    collection: str

@dataclass(frozen=True)
class FileStarted:
    filename: str
    index: int
    total: int

@dataclass(frozen=True)
class ChunkCreated:
    filename: str
    section_title: str
    chunk_index: int

@dataclass(frozen=True)
class BatchStored:
    batch_size: int
    total_stored: int

@dataclass(frozen=True)
class FileCompleted:
    filename: str
    chunks: int
    index: int
    total: int

@dataclass(frozen=True)
class IndexingDone:
    total_files: int
    total_chunks: int

IndexEvent = Union[
    ManifestChecked, IndexingSkipped, CollectionPrepared,
    FileStarted, ChunkCreated, BatchStored, FileCompleted,
    IndexingDone,
]


# ---------------------------------------------------------------------------
# Стадия 1: проверка манифеста
# ---------------------------------------------------------------------------

class ManifestCheckStage(PipelineStage[IndexContext, IndexEvent]):
    """Вычислить manifest, сравнить с сохранённым. Если не изменился — skip."""

    _HASH_CHUNK_SIZE = 8192

    @property
    def name(self) -> str:
        return "manifest_check"

    def run(self, ctx: IndexContext) -> Iterator[IndexEvent]:
        ctx.current_manifest = self._compute_manifest(ctx.source_path, ctx.reader_registry)
        stored = self._load_manifest(ctx.manifest_path)
        changed = ctx.current_manifest != stored

        yield ManifestChecked(files_changed=changed, file_count=len(ctx.current_manifest))

        if not changed:
            doc_count = ctx.vectorstore_service.collection_doc_count(ctx.collection_name)
            if doc_count > 0:
                ctx.skipped = True
                yield IndexingSkipped(collection=ctx.collection_name, doc_count=doc_count)

    @classmethod
    def _compute_manifest(cls, folder: Path, reader_registry: DocumentReaderRegistry) -> dict[str, str]:
        manifest: dict[str, str] = {}
        for f in reader_registry.iter_files(folder):
            manifest[f.name] = cls._file_hash(f)
        return manifest

    @classmethod
    def _file_hash(cls, file_path: Path) -> str:
        size = file_path.stat().st_size
        h = hashlib.sha256()
        h.update(str(size).encode())
        with open(file_path, "rb") as fh:
            head = fh.read(cls._HASH_CHUNK_SIZE)
            h.update(head)
            if size > 2 * cls._HASH_CHUNK_SIZE:
                fh.seek(-cls._HASH_CHUNK_SIZE, 2)
            tail = fh.read(cls._HASH_CHUNK_SIZE)
            if tail != head:
                h.update(tail)
        return h.hexdigest()

    @classmethod
    def _load_manifest(cls, manifest_path: Path) -> dict[str, str]:
        if not manifest_path.exists():
            return {}
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

# ---------------------------------------------------------------------------
# Стадия 2: подготовка коллекции
# ---------------------------------------------------------------------------

class CollectionPrepStage(PipelineStage[IndexContext, IndexEvent]):
    """Пересоздать коллекцию. Пропускается если skipped."""

    @property
    def name(self) -> str:
        return "collection_prep"

    def run(self, ctx: IndexContext) -> Iterator[IndexEvent]:
        if ctx.skipped:
            return

        vs = ctx.vectorstore_service
        if vs.collection_doc_count(ctx.collection_name) > 0:
            vs.remove_collection(ctx.collection_name)
        vs.create_collection(ctx.collection_name, ctx.embedding_model)

        yield CollectionPrepared(collection=ctx.collection_name)


# ---------------------------------------------------------------------------
# Стадия 3: индексация файлов
# ---------------------------------------------------------------------------

class FileIndexStage(PipelineStage[IndexContext, IndexEvent]):
    """Потоковая индексация: файл → чанки → батчи → vectorstore."""
    _STORE_BATCH_SIZE = 32

    @property
    def name(self) -> str:
        return "file_index"

    def run(self, ctx: IndexContext) -> Iterator[IndexEvent]:
        if ctx.skipped:
            return

        file_count = len(ctx.current_manifest)
        if file_count == 0:
            yield IndexingDone(total_files=0, total_chunks=0)
            return

        vs = ctx.vectorstore_service
        total_stored = 0
        batch: List[Document] = []

        for file_path in ctx.reader_registry.iter_files(ctx.source_path):
            ctx.total_files += 1
            file_chunks = 0

            yield FileStarted(filename=file_path.name, index=ctx.total_files, total=file_count)

            for chunk in ctx.reader_registry.iter_chunks(file_path):
                batch.append(chunk)
                file_chunks += 1

                yield ChunkCreated(
                    filename=file_path.name,
                    section_title=chunk.metadata.get("section_title", ""),
                    chunk_index=file_chunks,
                )

                if len(batch) >= self._STORE_BATCH_SIZE:
                    vs.store_batch(ctx.collection_name, batch)
                    total_stored += len(batch)
                    yield BatchStored(batch_size=len(batch), total_stored=total_stored)
                    batch.clear()

            ctx.total_chunks += file_chunks

            yield FileCompleted(
                filename=file_path.name,
                chunks=file_chunks,
                index=ctx.total_files,
                total=file_count,
            )

        # Финальный батч
        if batch:
            vs.store_batch(ctx.collection_name, batch)
            total_stored += len(batch)
            yield BatchStored(batch_size=len(batch), total_stored=total_stored)

        yield IndexingDone(total_files=ctx.total_files, total_chunks=ctx.total_chunks)


# ---------------------------------------------------------------------------
# Стадия 4: сохранение манифеста
# ---------------------------------------------------------------------------

class ManifestSaveStage(PipelineStage[IndexContext, IndexEvent]):
    """Сохранить манифест на диск. Пропускается если skipped."""

    @property
    def name(self) -> str:
        return "manifest_save"

    def run(self, ctx: IndexContext) -> Iterator[IndexEvent]:
        if not ctx.skipped:
            self._save_manifest(ctx.manifest_path, ctx.current_manifest)
        yield from ()

    @staticmethod
    def _save_manifest(manifest_path: Path, manifest: dict[str, str]) -> None:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Pipeline — оркестратор
# ---------------------------------------------------------------------------

def run_indexing(ctx: IndexContext) -> Iterator[IndexEvent]:
    """Запустить индексацию. Интерфейс не изменился — обратная совместимость."""
    yield from Pipeline([
        ManifestCheckStage(),
        CollectionPrepStage(),
        FileIndexStage(),
        ManifestSaveStage(),
    ]).run(ctx)

