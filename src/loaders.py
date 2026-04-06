"""Иерархия загрузчиков Confluence — генераторный стриминг.

Архитектура (композиция снизу вверх):
    PageLoader          — загрузка одной страницы
    BatchPageLoader     — загрузка списка страниц (yield PageLoaded/PageFailed)
    SpaceLoader         — загрузка пространства (yield SpaceEnumerated + delegate)

Pipeline-оркестраторы вынесены в load_pipeline/.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, List, Union

import requests
from langchain_community.document_loaders import ConfluenceLoader
from langchain_community.document_loaders.confluence import ContentFormat
from langchain_core.documents import Document

from errors import PageLoadError, SpaceEnumerationError
from load_pipeline.events import (
    LoadingDone,
    LoadPipelineEvent,
    PageFailed,
    PageLoaded,
    SpaceEnumerated,
)
from ui.state import AppConfig, ChunkingParams, SpaceLoadParams, StorageParams

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

    def load(self, page_ids: List[str]) -> Iterator[Union[PageLoaded, PageFailed, LoadingDone]]:
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

    def load(self, space_key: str) -> Iterator[Union[PageLoaded, PageFailed, LoadingDone, SpaceEnumerated]]:
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
# Обратно-совместимые точки входа (делегируют load_pipeline)
# ---------------------------------------------------------------------------

def run_page_pipeline(
    page_ids: List[str],
    collection_name: str,
    cfg: AppConfig,
    chunking_params: ChunkingParams,
    storage_params: StorageParams,
) -> Iterator[LoadPipelineEvent]:
    """Полный пайплайн: загрузка по page IDs -> чанкинг -> сохранение."""
    from load_pipeline import create_default_load_pipeline, create_load_context

    pipeline = create_default_load_pipeline()
    ctx = create_load_context(
        collection_name=collection_name,
        cfg=cfg,
        chunking_params=chunking_params,
        storage_params=storage_params,
        page_ids=page_ids,
    )
    yield from pipeline.run(ctx)


def run_space_pipeline(
    space_key: str,
    collection_name: str,
    cfg: AppConfig,
    space_params: SpaceLoadParams,
    chunking_params: ChunkingParams,
    storage_params: StorageParams,
) -> Iterator[LoadPipelineEvent]:
    """Полный пайплайн: загрузка пространства -> чанкинг -> сохранение."""
    from load_pipeline import create_default_load_pipeline, create_load_context

    pipeline = create_default_load_pipeline()
    ctx = create_load_context(
        collection_name=collection_name,
        cfg=cfg,
        chunking_params=chunking_params,
        storage_params=storage_params,
        space_key=space_key,
        space_params=space_params,
    )
    yield from pipeline.run(ctx)
