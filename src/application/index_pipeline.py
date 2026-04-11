"""Streaming index pipeline — индексация документов в векторную базу.

Полностью потоковый: каждый шаг (файл, чанк, сохранение) — yield.
Отдельный от doc-пайплайна, но используется им как первая стадия.

    for event in run_indexing(ctx):
        match event:
            case FileStarted(...): ...
            case ChunkCreated(...): ...
            case BatchStored(...): ...
            case FileCompleted(...): ...
            case IndexingDone(...): ...
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Union

from langchain_core.documents import Document

from application.readers.registry import registry
from domain.search.vectorstore import VectorStoreService

log = logging.getLogger(__name__)

_STORE_BATCH_SIZE = 32
_HASH_CHUNK_SIZE = 8192


# ---------------------------------------------------------------------------
# Контекст индексации
# ---------------------------------------------------------------------------

@dataclass
class IndexContext:
    """Входные данные для индексации. Минимальный набор — без query/model."""
    source_path: Path
    manifest_path: Path
    collection_name: str
    embedding_model: str
    vectorstore_service: VectorStoreService


# ---------------------------------------------------------------------------
# События (каждый шаг pipeline — отдельное событие)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ManifestChecked:
    """Манифест проверен."""
    files_changed: bool
    file_count: int


@dataclass(frozen=True)
class IndexingSkipped:
    """Индекс актуален, переиндексация не нужна."""
    collection: str
    doc_count: int


@dataclass(frozen=True)
class CollectionPrepared:
    """Коллекция пересоздана, готова к записи."""
    collection: str


@dataclass(frozen=True)
class FileStarted:
    """Начата обработка файла."""
    filename: str
    index: int
    total: int


@dataclass(frozen=True)
class ChunkCreated:
    """Чанк создан из файла."""
    filename: str
    section_title: str
    chunk_index: int


@dataclass(frozen=True)
class BatchStored:
    """Батч чанков сохранён в векторную базу."""
    batch_size: int
    total_stored: int


@dataclass(frozen=True)
class FileCompleted:
    """Файл полностью проиндексирован."""
    filename: str
    chunks: int
    index: int
    total: int


@dataclass(frozen=True)
class IndexingDone:
    """Индексация завершена."""
    total_files: int
    total_chunks: int


IndexEvent = Union[
    ManifestChecked, IndexingSkipped, CollectionPrepared,
    FileStarted, ChunkCreated, BatchStored, FileCompleted,
    IndexingDone,
]


# ---------------------------------------------------------------------------
# Streaming pipeline
# ---------------------------------------------------------------------------

def run_indexing(ctx: IndexContext) -> Iterator[IndexEvent]:
    """Полностью потоковая индексация — yield на каждый шаг.

    Поток событий:
        ManifestChecked
        → IndexingSkipped (если актуален) | CollectionPrepared
        → (FileStarted → ChunkCreated* → BatchStored* → FileCompleted)*
        → IndexingDone
    """
    # 1. Проверка манифеста
    current_manifest = _compute_manifest(ctx.source_path)
    stored_manifest = _load_manifest(ctx.manifest_path)
    changed = current_manifest != stored_manifest

    yield ManifestChecked(files_changed=changed, file_count=len(current_manifest))

    # 2. Skip если актуален
    if not changed:
        doc_count = ctx.vectorstore_service.collection_doc_count(ctx.collection_name)
        if doc_count > 0:
            yield IndexingSkipped(collection=ctx.collection_name, doc_count=doc_count)
            return

    # 3. Пересоздать коллекцию
    vs = ctx.vectorstore_service
    _drop_collection_if_exists(vs, ctx.collection_name)
    vs.create_collection(ctx.collection_name, ctx.embedding_model)

    yield CollectionPrepared(collection=ctx.collection_name)

    # 4. Пустая папка
    file_count = len(current_manifest)
    if file_count == 0:
        _save_manifest(ctx.manifest_path, current_manifest)
        yield IndexingDone(total_files=0, total_chunks=0)
        return

    # 5. Потоковая индексация: файл → чанки → батчи → сохранение
    total_files = 0
    total_chunks = 0
    total_stored = 0
    batch: List[Document] = []

    for file_path in registry.iter_files(ctx.source_path):
        total_files += 1
        file_chunks = 0

        yield FileStarted(filename=file_path.name, index=total_files, total=file_count)

        for chunk in registry.iter_chunks(file_path):
            batch.append(chunk)
            file_chunks += 1

            yield ChunkCreated(
                filename=file_path.name,
                section_title=chunk.metadata.get("section_title", ""),
                chunk_index=file_chunks,
            )

            if len(batch) >= _STORE_BATCH_SIZE:
                vs.store_batch(ctx.collection_name, batch)
                total_stored += len(batch)
                yield BatchStored(batch_size=len(batch), total_stored=total_stored)
                batch.clear()

        total_chunks += file_chunks

        yield FileCompleted(
            filename=file_path.name,
            chunks=file_chunks,
            index=total_files,
            total=file_count,
        )

    # 6. Финальный батч
    if batch:
        vs.store_batch(ctx.collection_name, batch)
        total_stored += len(batch)
        yield BatchStored(batch_size=len(batch), total_stored=total_stored)
        batch.clear()

    # 7. Сохранить манифест
    _save_manifest(ctx.manifest_path, current_manifest)

    yield IndexingDone(total_files=total_files, total_chunks=total_chunks)


# ---------------------------------------------------------------------------
# Фабрика контекста
# ---------------------------------------------------------------------------

def create_index_context(folder_path: Path, services) -> IndexContext:  # noqa: ANN001
    """Создать контекст индексации из folder_path и AppServices."""
    cfg = services.cfg
    cfg.boba_path(folder_path).mkdir(exist_ok=True)

    chroma_path = str(cfg.chroma_path(folder_path))
    return IndexContext(
        source_path=folder_path,
        manifest_path=cfg.index_manifest_path(folder_path),
        collection_name=cfg.collection_name(folder_path.name),
        embedding_model=cfg.embedding_model,
        vectorstore_service=services.create_vectorstore(chroma_path),
    )


# ---------------------------------------------------------------------------
# Манифест — хеши файлов
# ---------------------------------------------------------------------------

def _compute_manifest(folder: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for f in registry.iter_files(folder):
        manifest[f.name] = _file_hash(f)
    return manifest


def _file_hash(file_path: Path) -> str:
    size = file_path.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(file_path, "rb") as fh:
        head = fh.read(_HASH_CHUNK_SIZE)
        h.update(head)
        if size > 2 * _HASH_CHUNK_SIZE:
            fh.seek(-_HASH_CHUNK_SIZE, 2)
        tail = fh.read(_HASH_CHUNK_SIZE)
        if tail != head:
            h.update(tail)
    return h.hexdigest()


def _load_manifest(manifest_path: Path) -> dict[str, str]:
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(manifest_path: Path, manifest: dict[str, str]) -> None:
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _drop_collection_if_exists(vs: VectorStoreService, name: str) -> None:
    if vs.collection_doc_count(name) > 0:
        vs.remove_collection(name)
