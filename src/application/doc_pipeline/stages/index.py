"""Стадия 1: Индексация файлов из папки.

Проверка актуальности — по хешам файлов (манифест).
Переиндексация при любом изменении: добавление, удаление, модификация файла.
Чтение и разбиение файлов — через markdown_reader.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Iterator, List

from langchain_core.documents import Document

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import (
    DocPipelineEvent,
    FileIndexed,
    IndexingDone,
    IndexingSkipped,
)
from application.doc_pipeline.markdown_reader import iter_chunks, iter_files
from domain.pipeline import StageCompleted, StageStarted

log = logging.getLogger(__name__)

_STORE_BATCH_SIZE = 32
_HASH_CHUNK_SIZE = 8192


class IndexStage:
    """Индексирует файлы из папки через VectorStoreService."""

    @property
    def name(self) -> str:
        return "index"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)

        current_manifest = _compute_manifest(ctx.context_path)
        stored_manifest = _load_manifest(ctx.manifest_path)

        if current_manifest == stored_manifest:
            doc_count = ctx.vectorstore_service.collection_doc_count(ctx.collection_name)
            if doc_count > 0:
                yield IndexingSkipped(
                    collection=ctx.collection_name, doc_count=doc_count,
                )
                yield StageCompleted(
                    stage=self.name,
                    detail=f"индекс актуален ({doc_count} чанков, {len(current_manifest)} файлов)",
                )
                return

        vs = ctx.vectorstore_service
        _drop_collection_if_exists(vs, ctx.collection_name)
        vs.create_collection(ctx.collection_name, ctx.embedding_model)

        file_count = len(current_manifest)
        if file_count == 0:
            _save_manifest(ctx.manifest_path, current_manifest)
            yield IndexingDone(total_files=0, total_chunks=0)
            yield StageCompleted(stage=self.name, detail="нет файлов для индексации")
            return

        total_files = 0
        total_chunks = 0
        batch: List[Document] = []

        for file_path in iter_files(ctx.context_path):
            total_files += 1
            file_chunks = 0

            for chunk in iter_chunks(file_path):
                batch.append(chunk)
                file_chunks += 1

                if len(batch) >= _STORE_BATCH_SIZE:
                    vs.store_batch(ctx.collection_name, batch)
                    batch.clear()

            total_chunks += file_chunks
            yield FileIndexed(
                filename=file_path.name,
                chunks=file_chunks,
                index=total_files,
                total=file_count,
            )

        if batch:
            vs.store_batch(ctx.collection_name, batch)
            batch.clear()

        _save_manifest(ctx.manifest_path, current_manifest)
        yield IndexingDone(total_files=total_files, total_chunks=total_chunks)
        yield StageCompleted(stage=self.name, detail=f"{total_files} файлов, {total_chunks} чанков")


# ---------------------------------------------------------------------------
# Манифест — хеши файлов
# ---------------------------------------------------------------------------

def _compute_manifest(folder: Path) -> dict[str, str]:
    """Вычислить хеши всех индексируемых файлов в папке."""
    manifest: dict[str, str] = {}
    for f in iter_files(folder):
        manifest[f.name] = _file_hash(f)
    return manifest


def _file_hash(file_path: Path) -> str:
    """Быстрый fingerprint файла: размер + SHA-256 головы и хвоста."""
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
    """Загрузить сохранённый манифест. Пустой dict если файла нет."""
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(manifest_path: Path, manifest: dict[str, str]) -> None:
    """Сохранить манифест на диск."""
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _drop_collection_if_exists(vs, collection_name: str) -> None:  # noqa: ANN001
    """Удалить коллекцию если существует (для переиндексации)."""
    if vs.collection_doc_count(collection_name) > 0:
        vs.remove_collection(collection_name)
