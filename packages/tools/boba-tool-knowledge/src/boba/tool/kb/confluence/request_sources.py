"""Доступ к Confluence Server REST: URL-builder, пагинатор, RequestSource'ы.

- ConfluenceRest        — @staticmethod-фабрики путей и HttpRequest'ов
  (content/space/cql пути, page/attachment-запросы).
- ConfluencePaginator   — httpx-клиент для пагинированных discovery-запросов
  (+ discover_spaces/discover_space_pages/discover_pages_by_cql).
- RequestSource'ы       — CQL / Pages / Space / MultiSpace (discovery для
  ingest), и CqlSearch (один запрос на /content/search для online-tool'ов).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar
from urllib.parse import quote, urlparse

from pydantic import BaseModel

from boba.indexing import (
    Metadata,
    ReaderKeys,
    RequestSource,
    TransportKeys,
)
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.models import (
    AttachmentInfo,
    ConfluenceKeys,
    ConfluencePageItem,
    ConfluenceSpaceItem,
)
from boba.tool.kb.confluence.parsing import ConfluenceJson
from boba.tool.kb.indexing_log import Elapsed, IngestProgress
from boba.transport.http import CancellableHttpTransport, HttpRequest

__all__ = [
    "ConfluenceCqlRequestSource",
    "ConfluenceMultiSpaceRequestSource",
    "ConfluencePagesRequestSource",
    "ConfluencePaginator",
    "ConfluenceRequest",
    "ConfluenceRest",
    "ConfluenceSpaceRequestSource",
]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ConfluenceRequest:
    """Индексационный план запроса Confluence: чистый HTTP + metadata.

    Удовлетворяет Request-протокол (только metadata). source_id НЕ часть запроса:
    его выводит транспорт из реально запрашиваемого URL (base_url профиля +
    http.url, без волатильного query) — см. ConfluenceHttpTransport.source_id.
    Поэтому http.url несёт только path; base_url знает профиль/транспорт.
    Логические/презентационные URL (viewpage и т.п.) живут в metadata.

    http — чистый HttpRequest (path), который исполняет HttpTransport. Обогащение
    metadata данными ответа делает ConfluenceHttpTransport при сборке RawDocument.
    """

    http: HttpRequest
    metadata: Metadata = field(default_factory=Metadata.empty)


class ConfluenceRest:
    """Фабрики Confluence REST: URL/path-builders и HttpRequest-конструкторы."""

    DEFAULT_PAGE_LIMIT: ClassVar[int] = 50

    @staticmethod
    def page_fetch_path(page_id: str, *, body_format: str) -> str:
        expand = (
            f"body.{body_format},version,ancestors,space,metadata.labels,"
            "children.attachment.version,children.attachment.extensions"
        )
        return f"/rest/api/content/{page_id}?expand={expand}"

    @staticmethod
    def space_list_path(
        space_type: str,
        *,
        expand: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> str:
        type_filter = "" if space_type == "any" else f"&type={space_type}"
        expand_q = f"&expand={expand}" if expand else ""
        return f"/rest/api/space?limit={limit}&start=0{type_filter}{expand_q}"

    @staticmethod
    def space_pages_path(
        space_key: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> str:
        return f"/rest/api/space/{space_key}/content?type=page&limit={limit}&start=0"

    @staticmethod
    def cql_search_path(
        cql: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        expand: str | None = None,
    ) -> str:
        expand_q = f"&expand={expand}" if expand else ""
        cql_q = quote(cql, safe="")
        return f"/rest/api/content/search?cql={cql_q}&limit={limit}{expand_q}"

    @staticmethod
    def extract_host(base_url: str) -> str:
        netloc = urlparse(base_url).netloc
        return netloc or base_url.split("://", 1)[-1].split("/", 1)[0]

    @staticmethod
    def make_page_request(
        *,
        host: str,
        page_id: str,
        body_format: str,
    ) -> ConfluenceRequest:
        path = ConfluenceRest.page_fetch_path(page_id, body_format=body_format)
        return ConfluenceRequest(
            http=HttpRequest(url=path, method="GET"),
            metadata=(
                Metadata.empty()
                .set(ConfluenceKeys.PAGE_ID, page_id)
                .set(ConfluenceKeys.HOST, host)
            ),
        )

    @staticmethod
    def make_attachment_request(
        *,
        base_url: str,
        parent_metadata: Metadata,
        attachment: AttachmentInfo,
    ) -> ConfluenceRequest:
        meta = (
            Metadata.empty()
            .set(ConfluenceKeys.ATTACHMENT_INFO, attachment)
            .set(TransportKeys.CONTENT_TYPE, attachment.media_type)
            .set(ReaderKeys.PAGE_TITLE, attachment.title)
        )
        if (page_id := parent_metadata.get(ConfluenceKeys.PAGE_ID)) is not None:
            meta = meta.set(ConfluenceKeys.PAGE_ID, page_id)
        if (host := parent_metadata.get(ConfluenceKeys.HOST)) is not None:
            meta = meta.set(ConfluenceKeys.HOST, host)
        if (space := parent_metadata.get(ConfluenceKeys.SPACE_KEY)) is not None:
            meta = meta.set(ConfluenceKeys.SPACE_KEY, space)
        ancestors = parent_metadata.get(ConfluenceKeys.ANCESTORS_TITLES)
        if ancestors is not None:
            meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, ancestors)
        if (parent_url := parent_metadata.get(ConfluenceKeys.SOURCE_URL)) is not None:
            meta = meta.set(ConfluenceKeys.PARENT_URL, parent_url)
        base = base_url.rstrip("/")
        att_url = (
            f"{base}{attachment.webui}"
            if attachment.webui
            else f"{base}{attachment.download_path}"
        )
        meta = meta.set(ConfluenceKeys.SOURCE_URL, att_url)
        return ConfluenceRequest(
            http=HttpRequest(url=attachment.download_path, method="GET"),
            metadata=meta,
        )


class ConfluencePaginator:
    """httpx-клиент для пагинированных Confluence REST discovery-запросов.

    Каждый постраничный GET повторяется до retry_attempts раз (из web-профиля)
    на 5xx и transport-ошибках (timeout/connect) с линейным backoff'ом — большие
    Confluence (напр. Apache cwiki) отдают нестабильные 500 на глубокой
    пагинации. 4xx (клиентские) не ретраятся. После исчерпания попыток
    исключение пробрасывается наверх (caller решает: fail или degrade).

    Исполнение и retry (5xx/transport) — внутри HttpTransport, собранного из
    conn.profile; пагинатор лишь строит path и парсит JSON, а base_url к нему
    подставляет httpx-клиент из профиля (CancellableHttpTransport(base_url=...)).
    """

    def __init__(self, conn: ConfluenceConnection):
        self._http = CancellableHttpTransport(conn.profile)

    async def __call__(self, path: str, item: type[T]) -> AsyncIterator[T]:
        next_path: str | None = path
        while next_path:
            data = await self._get_json(next_path)
            results = ConfluenceJson.results(data)
            next_path = ConfluenceJson.next_link(data)
            logger.info(
                "discovery page: %d items, next=%s",
                len(results),
                bool(next_path),
            )
            for raw in results:
                yield item.model_validate(raw)

    async def _get_json(self, path: str) -> dict[str, Any]:
        logger.info("discovery request: GET %s", path)
        elapsed = Elapsed()
        async with self._http.fetch(HttpRequest(url=path)) as resp:
            payload = await resp.stream.read()

        logger.info(
            "discovery response: %d bytes in %dms", len(payload), elapsed.ms()
        )
        return json.loads(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._http.close()

    @classmethod
    async def discover_spaces(
        cls,
        conn: ConfluenceConnection,
        space_type: str,
    ) -> AsyncIterator[str]:
        async with cls(conn) as paginator:
            async for item in paginator(
                ConfluenceRest.space_list_path(space_type),
                ConfluenceSpaceItem,
            ):
                if item.key:
                    yield item.key

    @classmethod
    async def discover_space_pages(
        cls,
        conn: ConfluenceConnection,
        space_key: str,
    ) -> AsyncIterator[str]:
        async with cls(conn) as paginator:
            async for item in paginator(
                ConfluenceRest.space_pages_path(space_key),
                ConfluencePageItem,
            ):
                page_id = item.id.strip()
                if page_id:
                    yield page_id

    @classmethod
    async def discover_pages_by_cql(
        cls,
        conn: ConfluenceConnection,
        cql: str,
    ) -> AsyncIterator[str]:
        async with cls(conn) as paginator:
            async for item in paginator(
                ConfluenceRest.cql_search_path(cql),
                ConfluencePageItem,
            ):
                page_id = item.id.strip()
                if page_id:
                    yield page_id


class ConfluenceCqlRequestSource(RequestSource[ConfluenceRequest]):
    """CQL-запрос: space = DOCS AND lastModified > '2024-01-01' и т.п.

    Discovery — через /rest/api/content/search?cql=... с пагинацией.
    """

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        cql: str,
        progress: IngestProgress,
        body_format: str = "export_view",
    ) -> None:
        self._conn = conn
        self._cql = cql
        self._body_format = body_format
        self._host = ConfluenceRest.extract_host(conn.base_url)
        self._progress = progress

    async def requests(self) -> AsyncIterator[ConfluenceRequest]:
        logger.info("discovery start: cql %s", self._cql)
        pages = ConfluencePaginator.discover_pages_by_cql(self._conn, self._cql)
        async for page_id in pages:
            self._progress.pages_found(1)
            yield ConfluenceRest.make_page_request(
                host=self._host,
                page_id=page_id,
                body_format=self._body_format,
            )

        self._progress.pages_closed()


class ConfluencePagesRequestSource(RequestSource[ConfluenceRequest]):
    """Явный список page-id'ов; без discovery — page_ids фиксированы в ctor'е."""

    def __init__(
        self,
        *,
        base_url: str,
        page_ids: Sequence[str],
        body_format: str,
        progress: IngestProgress,
    ) -> None:
        self._host = ConfluenceRest.extract_host(base_url)
        self._page_ids = list(page_ids)
        self._body_format = body_format
        self._progress = progress

    async def requests(self) -> AsyncIterator[ConfluenceRequest]:
        """Список задан целиком: обнаружение закончено ещё до первого запроса."""
        self._progress.pages_found(len(self._page_ids))
        self._progress.pages_closed()

        for page_id in self._page_ids:
            yield ConfluenceRest.make_page_request(
                host=self._host,
                page_id=page_id,
                body_format=self._body_format,
            )


class ConfluenceSpaceRequestSource(RequestSource[ConfluenceRequest]):
    """Все страницы space через /rest/api/space/{key}/content."""

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        space_key: str,
        body_format: str,
        progress: IngestProgress,
    ) -> None:
        self._conn = conn
        self._space_key = space_key
        self._body_format = body_format
        self._host = ConfluenceRest.extract_host(conn.base_url)
        self._progress = progress

    @property
    def space_key(self) -> str:
        return self._space_key

    async def requests(self) -> AsyncIterator[ConfluenceRequest]:
        """Закрытие обнаружения — за владельцем обхода: space'ов может быть много."""
        logger.info("discovery start: space %s", self._space_key)
        pages = ConfluencePaginator.discover_space_pages(
            self._conn,
            self._space_key,
        )
        async for page_id in pages:
            self._progress.pages_found(1)
            yield ConfluenceRest.make_page_request(
                host=self._host,
                page_id=page_id,
                body_format=self._body_format,
            )


class ConfluenceMultiSpaceRequestSource(RequestSource[ConfluenceRequest]):
    """Все страницы из НЕСКОЛЬКИХ space'ов — последовательно через
    ConfluenceSpaceRequestSource для каждого ключа.

    Pipeline-семантика: всё ведёт себя как ОДНА выгрузка над union страниц.
    Cleanup идёт через touch-based mark (reconcile refresh'ит updated_at для
    всех виденных chunk'ов; FullCleanup сносит остальные).
    """

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        space_keys: Sequence[str],
        body_format: str,
        progress: IngestProgress,
    ) -> None:
        if not space_keys:
            raise ValueError("space_keys is empty")

        self._inner: list[ConfluenceSpaceRequestSource] = []
        for key in space_keys:
            self._inner.append(
                ConfluenceSpaceRequestSource(
                    conn=conn,
                    space_key=key,
                    body_format=body_format,
                    progress=progress,
                )
            )

        self._space_keys = tuple(space_keys)
        self._host = ConfluenceRest.extract_host(conn.base_url)
        self._progress = progress
        progress.spaces_found(len(self._space_keys))

    async def requests(self) -> AsyncIterator[ConfluenceRequest]:
        """Space'ы идут подряд; страницы обнаружены полностью после последнего."""
        for src in self._inner:
            async for request in src.requests():
                yield request

            self._progress.space_done(src.space_key)

        self._progress.pages_closed()
