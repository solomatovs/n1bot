"""Иерархия загрузчиков Confluence и пайплайн импорта — генераторный стриминг.

Архитектура (композиция снизу вверх):
    PageLoader          — загрузка одной страницы
    BatchPageLoader     — загрузка списка страниц (yield PageLoaded/PageFailed)
    SpaceLoader         — загрузка пространства (yield SpaceEnumerated + delegate)

Pipeline-оркестраторы:
    run_page_pipeline   — загрузка по page IDs → чанкинг → сохранение
    run_space_pipeline  — загрузка по space key → чанкинг → сохранение
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, List

import chromadb.errors
import requests
from langchain_community.document_loaders import ConfluenceLoader
from langchain_community.document_loaders.confluence import ContentFormat
from langchain_core.documents import Document

from chunking import AdvancedChunker, ChunkingStrategy
from errors import PageLoadError, SpaceEnumerationError
from events import (
    ChunkingDone,
    ChunkProduced,
    LoadingDone,
    PageFailed,
    PageLoaded,
    PipelineEvent,
    SectionChunked,
    SpaceEnumerated,
    StorageDone,
    StoreBatchDone,
    StoreBatchFailed,
)
from ui.state import AppConfig, ChunkingParams, SpaceLoadParams, StorageParams
from vectorstore import VectorStoreService

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Результат загрузки одной страницы (внутренний)
# ---------------------------------------------------------------------------

@dataclass
class PageResult:
    """Результат загрузки одной страницы."""
    page_id: str
    documents: List[Document]


# ---------------------------------------------------------------------------
# PageLoader — загрузка одной страницы
# ---------------------------------------------------------------------------

class PageLoader:
    """Загружает одну страницу Confluence по ID."""

    def __init__(self, cfg: AppConfig) -> None:
        self._base_url = cfg.confluence_url
        self._token = cfg.confluence_token

    def load(self, page_id: str) -> PageResult:
        """Загрузить одну страницу.

        Raises:
            PageLoadError: если страницу не удалось загрузить.
        """
        try:
            loader = ConfluenceLoader(
                url=self._base_url,
                token=self._token,
                include_attachments=False,
                keep_markdown_format=True,
                content_format=ContentFormat.EXPORT_VIEW,
                page_ids=[page_id],
                confluence_kwargs={"verify_ssl": False},
                limit=1,
            )
            docs = loader.load()
            return PageResult(page_id=page_id, documents=docs)
        except Exception as e:
            raise PageLoadError(page_id, e) from e


# ---------------------------------------------------------------------------
# BatchPageLoader — загрузка списка страниц (генератор)
# ---------------------------------------------------------------------------

class BatchPageLoader:
    """Загружает несколько страниц, yield событие на каждую."""

    def __init__(self, page_loader: PageLoader) -> None:
        self._page_loader = page_loader

    def load(self, page_ids: List[str]) -> Iterator[PipelineEvent]:
        """Yield PageLoaded | PageFailed на каждую страницу, затем LoadingDone."""
        total = len(page_ids)
        ok_count = 0
        failed_count = 0

        for idx, pid in enumerate(page_ids, start=1):
            try:
                result = self._page_loader.load(pid)
                ok_count += 1
                yield PageLoaded(
                    page_id=pid,
                    documents=result.documents,
                    index=idx,
                    total=total,
                )
            except PageLoadError as e:
                log.warning("Не удалось загрузить страницу %s: %s", pid, e.cause)
                failed_count += 1
                yield PageFailed(
                    page_id=pid,
                    error=e.cause,
                    index=idx,
                    total=total,
                )

        yield LoadingDone(ok_count=ok_count, failed_count=failed_count)


# ---------------------------------------------------------------------------
# SpaceLoader — загрузка пространства (генератор)
# ---------------------------------------------------------------------------

class SpaceLoader:
    """Загружает все страницы пространства — yield SpaceEnumerated, затем delegate."""

    def __init__(
        self,
        batch_loader: BatchPageLoader,
        cfg: AppConfig,
        params: SpaceLoadParams,
    ) -> None:
        self._batch_loader = batch_loader
        self._base_url = cfg.confluence_url
        self._token = cfg.confluence_token
        self._params = params

    def load(self, space_key: str) -> Iterator[PipelineEvent]:
        """Yield SpaceEnumerated, затем делегирует BatchPageLoader.

        Raises:
            SpaceEnumerationError: если не удалось получить список страниц.
        """
        page_ids = self._enumerate_page_ids(space_key)

        if self._params.max_pages is not None and self._params.max_pages > 0:
            page_ids = page_ids[:self._params.max_pages]

        yield SpaceEnumerated(space_key=space_key, total=len(page_ids))
        yield from self._batch_loader.load(page_ids)

    def _enumerate_page_ids(self, space_key: str) -> List[str]:
        try:
            return self._paginate_space(space_key)
        except (requests.RequestException, KeyError, ValueError) as e:
            raise SpaceEnumerationError(space_key, e) from e

    def _paginate_space(self, space_key: str) -> List[str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        limit = self._params.api_page_limit
        start = 0
        ids: List[str] = []

        while True:
            params = {"spaceKey": space_key, "type": "page", "limit": limit, "start": start}
            r = requests.get(
                f"{self._base_url.rstrip('/')}/rest/api/content",
                headers=headers,
                params=params,
                verify=False,
                timeout=20,
            )
            r.raise_for_status()
            data = r.json() or {}
            results = data.get("results", []) or []
            if not results:
                break
            ids.extend(str(it["id"]) for it in results if "id" in it)
            if len(results) < limit:
                break
            start += len(results)

        return ids


# ---------------------------------------------------------------------------
# Pipeline-оркестраторы (per-page streaming, без аккумуляторов)
# ---------------------------------------------------------------------------

def _streaming_pipeline(
    loading_events: Iterator[PipelineEvent],
    chunker: ChunkingStrategy,
    vs_service: VectorStoreService,
    collection_name: str,
    storage_params: StorageParams,
) -> Iterator[PipelineEvent]:
    """Общий потоковый пайплайн: загрузка → чанкинг → сохранение.

    Обрабатывает постранично: загрузил страницу → чанки → в батч → сохранил.
    В памяти только текущая страница + текущий батч.
    """
    vs_service.verify_embedding_connection()
    vectorstore = vs_service.get_vectorstore(collection_name)
    batch_size = storage_params.batch_size

    batch: list[Document] = []
    batch_idx = 0
    total_stored = 0
    total_failed = 0
    cumulative_chunks = 0
    has_documents = False

    for event in loading_events:
        yield event

        if not isinstance(event, PageLoaded):
            continue

        if not event.documents:
            continue

        has_documents = True

        # Чанкинг текущей страницы — yield события, собираем чанки в батч
        for chunk_event in chunker.split_documents(event.documents):
            if isinstance(chunk_event, SectionChunked):
                yield SectionChunked(
                    doc_index=event.index,
                    doc_total=event.total,
                    section_index=chunk_event.section_index,
                    section_total=chunk_event.section_total,
                    section_title=chunk_event.section_title,
                )
            elif isinstance(chunk_event, ChunkProduced):
                cumulative_chunks += 1
                yield ChunkProduced(chunk=chunk_event.chunk, cumulative_chunks=cumulative_chunks)
                batch.append(chunk_event.chunk)

                # Батч полный — сохраняем
                if len(batch) >= batch_size:
                    yield from _flush_batch(
                        vs_service, vectorstore, batch, batch_idx,
                        total_stored, total_failed, cumulative_chunks,
                    )
                    total_stored += len(batch)
                    batch = []
                    batch_idx += 1

    # Дочищаем остаток батча
    if batch:
        yield from _flush_batch(
            vs_service, vectorstore, batch, batch_idx,
            total_stored, total_failed, cumulative_chunks,
        )
        total_stored += len(batch)

    if has_documents and cumulative_chunks > 0:
        yield ChunkingDone(total_chunks=cumulative_chunks)

    yield StorageDone(total_stored=total_stored, total_failed=total_failed)


def _flush_batch(
    vs_service: VectorStoreService,
    vectorstore,  # noqa: ANN001
    batch: list[Document],
    batch_idx: int,
    total_stored: int,
    total_failed: int,
    total_chunks: int,
) -> Iterator[PipelineEvent]:
    """Сохранить один батч, yield результат."""
    try:
        vs_service.store_batch(vectorstore, batch)
        yield StoreBatchDone(
            batch_index=batch_idx,
            total_stored=total_stored + len(batch),
            total_chunks=total_chunks,
        )
    except chromadb.errors.ChromaError as e:
        log.warning("Батч %d не удалось сохранить: %s", batch_idx, e)
        yield StoreBatchFailed(
            batch_index=batch_idx,
            error=str(e),
            total_failed=total_failed + len(batch),
            total_chunks=total_chunks,
        )


def run_page_pipeline(
    page_ids: List[str],
    collection_name: str,
    cfg: AppConfig,
    chunking_params: ChunkingParams,
    storage_params: StorageParams,
) -> Iterator[PipelineEvent]:
    """Полный пайплайн: загрузка по page IDs → чанкинг → сохранение (per-page streaming)."""
    batch_loader = BatchPageLoader(PageLoader(cfg))
    chunker = AdvancedChunker(cfg, chunking_params)
    vs_service = VectorStoreService(cfg)

    yield from _streaming_pipeline(
        loading_events=batch_loader.load(page_ids),
        chunker=chunker,
        vs_service=vs_service,
        collection_name=collection_name,
        storage_params=storage_params,
    )


def run_space_pipeline(
    space_key: str,
    collection_name: str,
    cfg: AppConfig,
    space_params: SpaceLoadParams,
    chunking_params: ChunkingParams,
    storage_params: StorageParams,
) -> Iterator[PipelineEvent]:
    """Полный пайплайн: загрузка пространства → чанкинг → сохранение (per-page streaming)."""
    batch_loader = BatchPageLoader(PageLoader(cfg))
    space_loader = SpaceLoader(batch_loader, cfg, space_params)
    chunker = AdvancedChunker(cfg, chunking_params)
    vs_service = VectorStoreService(cfg)

    yield from _streaming_pipeline(
        loading_events=space_loader.load(space_key),
        chunker=chunker,
        vs_service=vs_service,
        collection_name=collection_name,
        storage_params=storage_params,
    )
