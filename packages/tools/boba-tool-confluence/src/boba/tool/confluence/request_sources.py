"""Доступ к Confluence Server REST: адреса, пагинатор, обходы и discovery.

- ConfluenceUrl        — сборка относительных адресов на httpx.URL.
- ConfluenceRest       — адреса запросов страниц, вложений, спейсов и поиска.
- ConfluencePaginator  — httpx-клиент пагинированных discovery-запросов.
- ContentListing       — откуда берётся список страниц: SpaceListing (список
  контента спейса), CqlListing (поиск по CQL), PageListing (одна страница).
- ConfluenceDiscovery  — RequestSource: страницы с версиями и вложениями без
  тел; запрос на каждую страницу и на каждое вложение, прошедшее гейт.

Ошибки:
TransportError — Confluence недоступен, ответил статусом или оборвал тело.
ConfluencePayloadError — ответ списка не разбирается как контент Confluence.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from boba.confluence.models import (
    AttachmentBlock,
    AttachmentGate,
    AttachmentInfo,
    AttachmentVerdict,
    ConfluenceAttachmentItem,
    ConfluenceContent,
    ConfluenceKeys,
    ConfluenceMarks,
    ConfluencePayloadError,
    ConfluenceSourceId,
    ConfluenceSpaceItem,
    ParseGrade,
)
from boba.confluence.parsing import ConfluenceJson
from boba.indexing import (
    Metadata,
    ReaderKeys,
    Request,
    RequestSource,
    SourceId,
    SourceMark,
    TransportError,
    TransportKeys,
)
from boba.tool.confluence.connection import ConfluenceConnection
from boba.tool.confluence.indexing_log import IngestProgress
from boba.toolkit.timing import Elapsed
from boba.transport.http import CancellableHttpTransport, HttpRequest
from boba.transport.http.profile import HttpConnection

__all__ = [
    "ConfluenceDiscovery",
    "ConfluencePaginator",
    "ConfluenceRequest",
    "ConfluenceRest",
    "ConfluenceUrl",
    "ContentListing",
    "CqlListing",
    "PageListing",
    "SpaceListing",
]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ConfluenceRequest(Request):
    """Индексационный план запроса Confluence: чистый HTTP, metadata, отметка.

    source_id НЕ часть запроса: его выводит транспорт из реально запрашиваемого
    URL (корень профиля + http.url, без волатильного query) через
    ConfluenceSourceId. Поэтому http.url несёт только path. mark — отпечаток
    версии из списка, по которому конвейер решает, качать ли тело.
    """

    http: HttpRequest
    mark: SourceMark
    metadata: Metadata = field(default_factory=Metadata.empty)


class ConfluenceUrl:
    """Сборка относительных адресов Confluence REST: сегменты пути и query.

    Единственное место, где адрес собирается: сегменты квотируются здесь
    (id и ключи приходят от LLM, `/`, `?`, `#` в них — просто байты), query
    кодирует httpx.URL. Ни один вызывающий строк не клеит.
    """

    ROOT: ClassVar[str] = "/rest/api"
    SEPARATOR: ClassVar[str] = "/"

    @classmethod
    def of(
        cls,
        *segments: str,
        params: Mapping[str, object] | None = None,
    ) -> httpx.URL:
        parts = [cls.ROOT]
        for segment in segments:
            parts.append(quote(segment, safe=""))

        path = cls.SEPARATOR.join(parts)
        if params is None:
            return httpx.URL(path=path)

        return httpx.URL(path=path, params=dict(params))

    @classmethod
    def link(cls, href: str) -> httpx.URL:
        """Ссылка `_links.next` от Confluence: она уже собрана и закодирована."""
        return httpx.URL(href)


class ConfluenceRest:
    """Фабрики Confluence REST: адреса запросов и HttpRequest-конструкторы."""

    DEFAULT_PAGE_LIMIT: ClassVar[int] = 50

    DISCOVERY_EXPAND: ClassVar[str] = (
        "version,space,ancestors,"
        "children.attachment.version,children.attachment.extensions"
    )
    """Что раскрывать в списке: версии и вложения без тел страниц."""

    ATTACHMENTS_EXPAND: ClassVar[str] = "version"

    UNKNOWN_VERSION: ClassVar[int] = 0
    """Версия страницы, которой обход не увидел: с записью реестра не совпадёт."""

    @staticmethod
    def page_fetch_path(page_id: str, *, body_format: str) -> httpx.URL:
        """Страница целиком: тело и вложения — для инструментов чтения."""
        expand = (
            f"body.{body_format},version,ancestors,space,metadata.labels,"
            "children.attachment.version,children.attachment.extensions"
        )
        return ConfluenceUrl.of("content", page_id, params={"expand": expand})

    @staticmethod
    def page_body_path(page_id: str, *, body_format: str) -> httpx.URL:
        """Тело страницы для индексации; вложения уже известны из списка."""
        expand = f"body.{body_format},version,ancestors,space,metadata.labels"
        return ConfluenceUrl.of("content", page_id, params={"expand": expand})

    @staticmethod
    def page_summary_path(page_id: str) -> httpx.URL:
        """Страница без тела: версия и вложения — обход по одной странице.

        Идёт мимо поиска, поэтому видит и страницы архивных спейсов.
        """
        return ConfluenceUrl.of(
            "content",
            page_id,
            params={"expand": ConfluenceRest.DISCOVERY_EXPAND},
        )

    @staticmethod
    def attachments_path(
        page_id: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> httpx.URL:
        """Полный список вложений страницы: раскрытие в списке ограничено."""
        return ConfluenceUrl.of(
            "content",
            page_id,
            "child",
            "attachment",
            params={
                "limit": limit,
                "start": 0,
                "expand": ConfluenceRest.ATTACHMENTS_EXPAND,
            },
        )

    @staticmethod
    def space_path(space_key: str) -> httpx.URL:
        """Один space: 404 на несуществующий ключ."""
        return ConfluenceUrl.of("space", space_key)

    @staticmethod
    def space_content_path(
        space_key: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> httpx.URL:
        """Страницы спейса списком из базы, а не из поискового индекса.

        Поиск не отдаёт контент архивных спейсов и отстаёт от только что
        созданных страниц; этот список знает и то, и другое.
        """
        return ConfluenceUrl.of(
            "space",
            space_key,
            "content",
            "page",
            params={
                "limit": limit,
                "start": 0,
                "expand": ConfluenceRest.DISCOVERY_EXPAND,
            },
        )

    @staticmethod
    def space_list_path(
        space_type: str,
        *,
        expand: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> httpx.URL:
        params: dict[str, object] = {"limit": limit, "start": 0}
        if space_type != "any":
            params["type"] = space_type

        if expand:
            params["expand"] = expand

        return ConfluenceUrl.of("space", params=params)

    @staticmethod
    def cql_search_path(
        cql: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        start: int = 0,
        expand: str | None = None,
    ) -> httpx.URL:
        params: dict[str, object] = {"cql": cql, "limit": limit, "start": start}
        if expand:
            params["expand"] = expand

        return ConfluenceUrl.of("content", "search", params=params)

    @staticmethod
    def make_page_request(
        *,
        profile: HttpConnection,
        content: ConfluenceContent,
        body_format: str,
    ) -> ConfluenceRequest:
        path = ConfluenceRest.page_body_path(content.id, body_format=body_format)
        meta = (
            Metadata.empty()
            .set(ConfluenceKeys.PAGE_ID, content.id)
            .set(ConfluenceKeys.HOST, profile.address_host())
        )
        return ConfluenceRequest(
            http=HttpRequest(url=str(path), method="GET"),
            mark=ConfluenceMarks.page(content.version.number),
            metadata=meta,
        )

    @staticmethod
    def make_gone_request(
        *,
        profile: HttpConnection,
        page_id: str,
        body_format: str,
    ) -> ConfluenceRequest:
        """Запрос страницы, которой обход не нашёл: ответ 404 снимет её с индекса.

        Отметка версии заведомо не совпадёт с записью реестра, поэтому конвейер
        сходит за телом и получит от Confluence прямой ответ, есть страница
        или нет.
        """
        path = ConfluenceRest.page_body_path(page_id, body_format=body_format)
        meta = (
            Metadata.empty()
            .set(ConfluenceKeys.PAGE_ID, page_id)
            .set(ConfluenceKeys.HOST, profile.address_host())
        )
        return ConfluenceRequest(
            http=HttpRequest(url=str(path), method="GET"),
            mark=ConfluenceMarks.page(ConfluenceRest.UNKNOWN_VERSION),
            metadata=meta,
        )

    @staticmethod
    def make_attachment_request(  # noqa: PLR0913 — адрес, родитель и режим врозь
        *,
        profile: HttpConnection,
        page: ConfluenceContent,
        page_source: SourceId,
        attachment: AttachmentInfo,
        grade: ParseGrade,
        skip: str = "",
    ) -> ConfluenceRequest:
        meta = (
            Metadata.empty()
            .set(ConfluenceKeys.ATTACHMENT_INFO, attachment)
            .set(TransportKeys.CONTENT_TYPE, attachment.media_type)
            .set(ReaderKeys.PAGE_TITLE, attachment.title)
            .set(ConfluenceKeys.PAGE_ID, page.id)
            .set(ConfluenceKeys.HOST, profile.address_host())
        )
        if page.space.key:
            meta = meta.set(ConfluenceKeys.SPACE_KEY, page.space.key)

        if ancestors := page.ancestor_titles():
            meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, ancestors)

        if page.links.webui:
            parent_url = str(profile.url_of(page.links.webui))
            meta = meta.set(ConfluenceKeys.PARENT_URL, parent_url)

        att_path = attachment.download_path
        if attachment.webui:
            att_path = attachment.webui

        meta = meta.set(ConfluenceKeys.SOURCE_URL, str(profile.url_of(att_path)))
        return ConfluenceRequest(
            http=HttpRequest(url=attachment.download_path, method="GET"),
            mark=ConfluenceMarks.attachment(
                attachment, parent=page_source, grade=grade, skip=skip
            ),
            metadata=meta,
        )


class ContentListing(ABC):
    """Откуда прогон берёт список страниц: спейс, запрос CQL или одна страница.

    Базовый класс трёх режимов обхода. Реализацию выбирает IngestScope и
    отдаёт ConfluenceDiscovery, который из полученных страниц строит запросы
    тел и вложений — сами режимы отличаются только источником списка.
    """

    @abstractmethod
    def label(self) -> str:
        """Что обходим — для логов и сообщений об ошибках."""
        ...

    @abstractmethod
    def contents(
        self, paginator: ConfluencePaginator
    ) -> AsyncIterator[ConfluenceContent]:
        """Страницы обхода: версии и вложения, без тел."""
        ...

    def missing(self) -> Sequence[str]:
        """Id страниц, которых источник данных не отдал; проверяет их конвейер."""
        return ()


class SpaceListing(ContentListing):
    """Страницы спейса списком контента, а не поиском.

    Поиск Confluence не видит архивные спейсы и отстаёт от свежих правок, а
    список спейса читает базу, поэтому обход спейса идёт им.
    """

    def __init__(self, space_key: str) -> None:
        self._space_key = space_key

    def label(self) -> str:
        return f"space {self._space_key}"

    def contents(
        self, paginator: ConfluencePaginator
    ) -> AsyncIterator[ConfluenceContent]:
        return paginator(
            ConfluenceRest.space_content_path(self._space_key),
            ConfluenceContent,
        )


class CqlListing(ContentListing):
    """Страницы выборки CQL: запрос задаёт вызывающий, поиск исполняет.

    Всё, что вне поискового индекса (архивные спейсы, только что созданные
    страницы), в такую выборку не попадает — это свойство самого поиска.
    """

    def __init__(self, cql: str) -> None:
        self._cql = cql

    @property
    def cql(self) -> str:
        return self._cql

    def label(self) -> str:
        return f"cql {self._cql}"

    def contents(
        self, paginator: ConfluencePaginator
    ) -> AsyncIterator[ConfluenceContent]:
        return paginator(
            ConfluenceRest.cql_search_path(
                self._cql,
                expand=ConfluenceRest.DISCOVERY_EXPAND,
            ),
            ConfluenceContent,
        )


class PageListing(ContentListing):
    """Одна страница по id: прямой запрос вместо поиска.

    Страница читается по адресу, поэтому режим работает и в архивном спейсе.
    Ответ 404 запоминается в missing(): страницы нет, и конвейер снимет её
    с индекса вместе с вложениями.
    """

    def __init__(self, page_id: str) -> None:
        self._page_id = page_id
        self._missing: list[str] = []

    def label(self) -> str:
        return f"page {self._page_id}"

    async def contents(
        self, paginator: ConfluencePaginator
    ) -> AsyncIterator[ConfluenceContent]:
        self._missing = []
        try:
            content = await paginator.one(
                ConfluenceRest.page_summary_path(self._page_id),
                ConfluenceContent,
            )
        except TransportError as exc:
            logger.info("page %s is not readable: %s", self._page_id, exc)
            self._missing.append(self._page_id)
            return

        yield content

    def missing(self) -> Sequence[str]:
        return tuple(self._missing)


class ConfluencePaginator:
    """httpx-клиент для пагинированных Confluence REST discovery-запросов.

    Исполнение и retry (5xx/transport) — внутри HttpTransport, собранного из
    conn.profile; пагинатор лишь строит path, ходит по `_links.next` и
    разбирает результаты в модель item.
    """

    def __init__(self, conn: ConfluenceConnection):
        self._http = CancellableHttpTransport(conn.profile)

    async def __call__(self, url: httpx.URL, item: type[T]) -> AsyncIterator[T]:
        next_url: httpx.URL | None = url
        while next_url is not None:
            data = await self.get_json(next_url)
            results = ConfluenceJson.results(data)
            next_url = self._next(data)
            logger.info(
                "discovery page: %d items, next=%s",
                len(results),
                next_url is not None,
            )
            for raw in results:
                yield self.item(item, raw, url)

    async def one(self, url: httpx.URL, item: type[T]) -> T:
        """Один объект вместо списка: содержимое ответа и есть результат."""
        data = await self.get_json(url)

        return self.item(item, data, url)

    @staticmethod
    def _next(data: dict[str, Any]) -> httpx.URL | None:
        link = ConfluenceJson.next_link(data)
        if not link:
            return None

        return ConfluenceUrl.link(link)

    @staticmethod
    def item(item: type[T], raw: dict[str, Any], url: httpx.URL) -> T:
        try:
            return item.model_validate(raw)
        except ValidationError as exc:
            msg = (
                f"confluence discovery: GET {url} expected {item.__name__} items, "
                f"got {json.dumps(raw)[:200]}: {exc}"
            )
            raise ConfluencePayloadError(msg) from exc

    async def get_json(self, url: httpx.URL) -> dict[str, Any]:
        """Один GET с разбором JSON: статус и обрыв уходят TransportError."""
        logger.info("discovery request: GET %s", url)
        elapsed = Elapsed()
        try:
            async with self._http.fetch(HttpRequest(url=str(url))) as resp:
                payload = await resp.stream.read()
        except httpx.HTTPError as exc:
            msg = f"GET {url} on confluence: {type(exc).__name__}: {exc}"
            raise TransportError(msg) from exc

        logger.info("discovery response: %d bytes in %dms", len(payload), elapsed.ms())
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            msg = (
                f"GET {url} on confluence: expected JSON, got {payload[:200]!r}: {exc}"
            )
            raise ConfluencePayloadError(msg) from exc

        return ConfluenceJson.as_dict(data)

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


class ConfluenceDiscovery(RequestSource[ConfluenceRequest]):
    """Обход по CQL: страницы с версиями и вложениями, без тел.

    На каждую страницу — запрос тела с отметкой версии; конвейер сам решит по
    реестру, нужно ли его исполнять. На каждое вложение — запрос скачивания с
    отпечатком из списка, а отсечённое гейтом уходит с причиной в отметке:
    конвейер его не скачивает, но помнит, что оно существует.
    """

    def __init__(
        self,
        *,
        conn: ConfluenceConnection,
        listing: ContentListing,
        gate: AttachmentGate,
        grade: ParseGrade,
        progress: IngestProgress,
    ) -> None:
        self._conn = conn
        self._listing = listing
        self._gate = gate
        self._grade = grade
        self._progress = progress

    @property
    def listing(self) -> ContentListing:
        return self._listing

    async def requests(self) -> AsyncIterator[ConfluenceRequest]:
        logger.info("discovery start: %s", self._listing.label())
        async with ConfluencePaginator(self._conn) as paginator:
            async for content in self._listing.contents(paginator):
                self._progress.pages_found(1)
                yield ConfluenceRest.make_page_request(
                    profile=self._conn.profile,
                    content=content,
                    body_format=self._conn.body_format,
                )

                async for request in self._attachment_requests(paginator, content):
                    yield request

            for page_id in self._listing.missing():
                yield ConfluenceRest.make_gone_request(
                    profile=self._conn.profile,
                    page_id=page_id,
                    body_format=self._conn.body_format,
                )

        self._progress.pages_closed()

    async def _attachment_requests(
        self,
        paginator: ConfluencePaginator,
        content: ConfluenceContent,
    ) -> AsyncIterator[ConfluenceRequest]:
        profile = self._conn.profile
        body_url = ConfluenceRest.page_body_path(
            content.id, body_format=self._conn.body_format
        )
        page_source = ConfluenceSourceId.of(profile, str(body_url))
        async for att in self._attachments(paginator, content):
            self._progress.attachments_found(1)
            verdict = self._gate.verdict(att)
            if verdict is not AttachmentVerdict.TAKE:
                logger.info(
                    "attachment skipped (%s): id=%s title=%r media_type=%r",
                    verdict.value,
                    att.id,
                    att.title,
                    att.media_type,
                )

            yield ConfluenceRest.make_attachment_request(
                profile=profile,
                page=content,
                page_source=page_source,
                attachment=att,
                grade=self._grade,
                skip=verdict.skipped(),
            )

    @staticmethod
    async def _attachments(
        paginator: ConfluencePaginator,
        content: ConfluenceContent,
    ) -> AsyncIterator[AttachmentInfo]:
        """Вложения страницы: из раскрытия списка, а при усечении — полным списком."""
        block: AttachmentBlock = content.children.attachment
        if not block.truncated():
            for item in block.results:
                yield item.info()

            return

        logger.info(
            "page %s: attachment expansion truncated at %d, listing all",
            content.id,
            block.size,
        )
        path = ConfluenceRest.attachments_path(content.id)
        async for item in paginator(path, ConfluenceAttachmentItem):
            yield item.info()
