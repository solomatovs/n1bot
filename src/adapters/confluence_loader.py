"""Иерархия загрузчиков Confluence — генераторный стриминг.

Архитектура (композиция снизу вверх):
    PageLoader          — ленивая загрузка документов одной страницы
    BatchPageLoader     — загрузка списка страниц (yield событий на каждый документ)
    SpaceLoader         — загрузка пространства (yield SpaceEnumerated + delegate)

Pipeline-оркестраторы вынесены в load_pipeline/.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, List, Union

import httpx
from langchain_community.document_loaders import ConfluenceLoader
from langchain_community.document_loaders.confluence import ContentFormat
from langchain_core.documents import Document

from application.load_pipeline.events import (
    DocumentLoaded,
    LoadingDone,
    PageFailed,
    PageLoaded,
    SpaceEnumerated,
)
from domain.config import AppConfig
from domain.loading import (
    ConfluenceLoaderParams,
    PageLoadError,
    SpaceEnumerationError,
    SpaceLoadParams,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PageLoader — ленивая загрузка документов одной страницы
# ---------------------------------------------------------------------------

class PageLoader:
    """Загружает документы одной страницы Confluence — yield по одному."""

    def __init__(self, cfg: AppConfig, params: ConfluenceLoaderParams) -> None:
        self._cfg = cfg
        self._params = params

    def load(self, page_id: str) -> Iterator[Document]:
        """Yield документы страницы по одному через lazy_load.

        Raises:
            PageLoadError: если страницу не удалось загрузить.
        """
        try:
            p = self._params
            loader = ConfluenceLoader(
                url=self._cfg.confluence_url,
                token=self._cfg.confluence_token,
                page_ids=[page_id],
                content_format=ContentFormat(p.content_format.value),
                keep_markdown_format=p.keep_markdown_format,
                keep_newlines=p.keep_newlines,
                include_attachments=p.include_attachments,
                include_comments=p.include_comments,
                include_labels=p.include_labels,
                number_of_retries=p.number_of_retries,
                min_retry_seconds=p.min_retry_seconds,
                max_retry_seconds=p.max_retry_seconds,
                confluence_kwargs={"verify_ssl": p.ssl_verify},
                limit=1,
            )
            yield from loader.lazy_load()
        except PageLoadError:
            raise
        except Exception as e:
            raise PageLoadError(page_id, e) from e


# ---------------------------------------------------------------------------
# BatchPageLoader — загрузка списка страниц (генератор)
# ---------------------------------------------------------------------------

class BatchPageLoader:
    """Загружает несколько страниц, yield событие на каждый документ."""

    def __init__(self, page_loader: PageLoader) -> None:
        self._page_loader = page_loader

    def load(self, page_ids: List[str]) -> Iterator[Union[PageLoaded, DocumentLoaded, PageFailed, LoadingDone]]:
        """Yield PageLoaded, затем DocumentLoaded на каждый документ, затем LoadingDone."""
        total = len(page_ids)
        ok_count = 0
        failed_count = 0

        for idx, pid in enumerate(page_ids, start=1):
            try:
                yield PageLoaded(page_id=pid, index=idx, total=total)
                for doc in self._page_loader.load(pid):
                    yield DocumentLoaded(
                        page_id=pid,
                        document=doc,
                        index=idx,
                        total=total,
                    )
                ok_count += 1
            except PageLoadError as e:
                log.warning("Failed to load page %s: %s", pid, e.cause)
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

@dataclass(frozen=True)
class ContentQuery:
    """Параметры запроса пагинации контента Confluence REST API."""
    space_key: str
    content_type: str = "page"
    limit: int = 50
    start: int = 0

    def to_params(self) -> dict[str, str | int]:
        return {"spaceKey": self.space_key, "type": self.content_type, "limit": self.limit, "start": self.start}


class SpaceLoader:
    """Загружает все страницы пространства — yield SpaceEnumerated, затем delegate."""

    def __init__(
        self,
        batch_loader: BatchPageLoader,
        cfg: AppConfig,
        params: SpaceLoadParams,
        loader_params: ConfluenceLoaderParams,
    ) -> None:
        self._batch_loader = batch_loader
        self._cfg = cfg
        self._params = params
        self._loader_params = loader_params

    def load(self, space_key: str) -> Iterator[Union[PageLoaded, DocumentLoaded, PageFailed, LoadingDone, SpaceEnumerated]]:
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
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise SpaceEnumerationError(space_key, e) from e

    def _paginate_space(self, space_key: str) -> List[str]:
        limit = self._params.api_page_limit
        start = 0
        ids: List[str] = []

        while True:
            query = ContentQuery(space_key=space_key, limit=limit, start=start)
            r = httpx.get(
                self._cfg.confluence_content_url,
                headers=self._cfg.confluence_auth_headers,
                params=query.to_params(),
                verify=self._loader_params.ssl_verify,
                timeout=self._loader_params.timeout,
            )
            r.raise_for_status()
            page_ids = self._extract_page_ids(r.json())
            if not page_ids:
                break
            ids.extend(page_ids)
            if len(page_ids) < limit:
                break
            start += len(page_ids)

        return ids

    @staticmethod
    def _extract_page_ids(response_json: dict) -> List[str]:
        """Извлечь ID страниц из ответа Confluence REST API."""
        results = response_json.get("results") or []
        return [str(item["id"]) for item in results if "id" in item]
