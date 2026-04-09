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

import httpx
from langchain_community.document_loaders import ConfluenceLoader
from langchain_community.document_loaders.confluence import ContentFormat
from langchain_core.documents import Document

from application.load_pipeline.events import (
    LoadingDone,
    PageFailed,
    PageLoaded,
    SpaceEnumerated,
)
from domain.config import AppConfig
from domain.loading import (
    ConfluenceRequestParams,
    PageLoadError,
    SpaceEnumerationError,
    SpaceLoadParams,
)

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

    def __init__(self, cfg: AppConfig, request_params: ConfluenceRequestParams) -> None:
        self._cfg = cfg
        self._request_params = request_params

    def load(self, page_id: str) -> PageResult:
        """Загрузить одну страницу.

        Raises:
            PageLoadError: если страницу не удалось загрузить.
        """
        try:
            loader = ConfluenceLoader(
                url=self._cfg.confluence_url,
                token=self._cfg.confluence_token,
                include_attachments=False,
                keep_markdown_format=True,
                content_format=ContentFormat.EXPORT_VIEW,
                page_ids=[page_id],
                confluence_kwargs={"verify_ssl": self._request_params.ssl_verify},
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

class SpaceLoader:
    """Загружает все страницы пространства — yield SpaceEnumerated, затем delegate."""

    def __init__(
        self,
        batch_loader: BatchPageLoader,
        cfg: AppConfig,
        params: SpaceLoadParams,
        request_params: ConfluenceRequestParams,
    ) -> None:
        self._batch_loader = batch_loader
        self._cfg = cfg
        self._params = params
        self._request_params = request_params

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
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise SpaceEnumerationError(space_key, e) from e

    def _paginate_space(self, space_key: str) -> List[str]:
        limit = self._params.api_page_limit
        start = 0
        ids: List[str] = []

        while True:
            query = {"spaceKey": space_key, "type": "page", "limit": limit, "start": start}
            r = httpx.get(
                self._cfg.confluence_content_url,
                headers=self._cfg.confluence_auth_headers,
                params=query,
                verify=self._request_params.ssl_verify,
                timeout=self._request_params.timeout,
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
