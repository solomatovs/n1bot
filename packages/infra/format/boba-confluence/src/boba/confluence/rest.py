"""Доступ к Confluence Server REST: endpoint, адреса, запросы и пагинатор.

- ConfluenceConnection — endpoint: web-профиль (адрес, auth, ретраи), формат
  тела и дамп обмена.
- CflUrlBuilder        — сборка относительных адресов на httpx.URL.
- CflRestBuilder       — адреса запросов страниц, вложений, спейсов и поиска;
  конструкторы ConfluenceRequest для конвейера индексации.
- CflRest              — GET, JSON и обход страниц выдачи поверх HttpTransport
  проекта; единственный клиент REST для тулов, конвейера и индексатора.
- CflPaginator         — модели из discovery-запросов поверх CflRest.

Ошибки:
TransportError — Confluence недоступен, ответил статусом или оборвал тело.
ConfluencePayloadError — ответ не разбирается как контент Confluence.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Literal, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationError

from boba.confluence.models import (
    AttachmentInfo,
    ConfluenceContent,
    ConfluenceKeys,
    ConfluenceMarks,
    ConfluencePayloadError,
    ParseGrade,
)
from boba.confluence.parsing import JsonNode
from boba.indexing import (
    Metadata,
    ReaderKeys,
    Request,
    SourceId,
    SourceMark,
    TransportError,
    TransportKeys,
)
from boba.toolkit.timing import Elapsed
from boba.transport.http import (
    CancellableHttpTransport,
    HttpRequest,
    HttpTransport,
    HttpTransportConfig,
)
from boba.transport.http.connection import HttpConnection

__all__ = [
    "CflPaginator",
    "CflRest",
    "CflRestBuilder",
    "CflUrlBuilder",
    "ConfluenceConnection",
    "ConfluenceRequest",
    "ContentType",
    "SpaceStatus",
    "SpaceType",
]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ConfluenceConnection(BaseModel):
    """Confluence endpoint: формат тела, профиль соединения и транспорт."""

    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description=(
            "`view` — clean HTML (рекомендуется); `export_view` — с макросами; "
            "`storage` — raw storage XML."
        ),
    )
    connection: HttpConnection = Field(
        description=(
            "Транспортный web-профиль (host/port/path/timeout/ssl/auth) ссылкой "
            '`connection = "${web.<name>}"`. Адрес Confluence задаётся там же; '
            "auth (PAT/Basic) — там же `auth = { method = 'bearer', token = '...' }`."
        ),
    )
    transport: HttpTransportConfig = Field(
        default_factory=HttpTransportConfig,
        description=(
            "Поведение HTTP-транспорта процесса: таймауты, пул, дамп обмена; "
            'ссылкой `transport = "${http}"`.'
        ),
    )


@dataclass(frozen=True)
class ConfluenceRequest(Request):
    """Индексационный план запроса Confluence: чистый HTTP, metadata, отметка.

    source_id НЕ часть запроса: его выводит транспорт из реально запрашиваемого
    URL (корень профиля + http.url, без волатильного query) через
    ConfluenceSourceIds. Поэтому http.url несёт только path. mark — отпечаток
    версии из списка, по которому конвейер решает, качать ли тело.
    """

    http: HttpRequest
    mark: SourceMark
    metadata: Metadata = field(default_factory=Metadata.empty)


class CflUrlBuilder:
    """
    Собирает rest url запросы к confluence
    Других сборщиков не должно быть
    это центральный компонент по сборке Confluence Rest Url
    """

    def __init__(self):
        self.root = "/rest/api"
        self.sep = "/"

    def url_of(
        self,
        *path_segments: str,
        params: Mapping[str, object] | None = None,
    ) -> httpx.URL:
        parts = [self.root]
        for segment in path_segments:
            parts.append(quote(segment, safe=""))

        path = self.sep.join(parts)
        if params is None:
            return httpx.URL(path=path)

        return httpx.URL(path=path, params=dict(params))

    def raw_to_url(self, href: str) -> httpx.URL:
        """Ссылка `_links.next` от Confluence: она уже собрана и закодирована."""
        return httpx.URL(href)


class ContentType(StrEnum):
    """Вид контента в списках спейса и в поле type ответа."""

    PAGE = "page"
    BLOGPOST = "blogpost"


class SpaceType(StrEnum):
    """Вид спейса в параметре type списка спейсов."""

    GLOBAL = "global"
    PERSONAL = "personal"
    ANY = "any"


class SpaceStatus(StrEnum):
    """Состояние спейса в ответе списка: у архивного контент читается, но поиск
    Confluence его не отдаёт."""

    CURRENT = "current"
    ARCHIVED = "archived"


class CflRestBuilder:
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

    def __init__(self):
        self._cub = CflUrlBuilder()
        self._marks = ConfluenceMarks()

    def page_fetch_path(self, page_id: str, *, body_format: str) -> httpx.URL:
        """Страница целиком: тело и вложения — для инструментов чтения."""
        expand = (
            f"body.{body_format},version,ancestors,space,metadata.labels,"
            "children.attachment.version,children.attachment.extensions"
        )
        return self._cub.url_of("content", page_id, params={"expand": expand})

    def page_body_path(self, page_id: str, *, body_format: str) -> httpx.URL:
        """Тело страницы для индексации; вложения уже известны из списка."""
        expand = f"body.{body_format},version,ancestors,space,metadata.labels,history"
        return self._cub.url_of("content", page_id, params={"expand": expand})

    def page_summary_path(self, page_id: str) -> httpx.URL:
        """Страница без тела: версия и вложения — обход по одной странице.

        Идёт мимо поиска, поэтому видит и страницы архивных спейсов.
        """
        return self._cub.url_of(
            "content",
            page_id,
            params={"expand": CflRestBuilder.DISCOVERY_EXPAND},
        )

    def attachments_path(
        self,
        page_id: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> httpx.URL:
        """Полный список вложений страницы: раскрытие в списке ограничено."""
        return self._cub.url_of(
            "content",
            page_id,
            "child",
            "attachment",
            params={
                "limit": limit,
                "start": 0,
                "expand": CflRestBuilder.ATTACHMENTS_EXPAND,
            },
        )

    def comments_path(
        self,
        page_id: str,
        *,
        expand: str,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> httpx.URL:
        """Комментарии страницы с телами: у страницы нет признака, что они
        менялись, поэтому список читается на каждом обходе."""
        return self._cub.url_of(
            "content",
            page_id,
            "child",
            "comment",
            params={"limit": limit, "start": 0, "expand": expand},
        )

    def space_path(self, space_key: str, *, expand: str = "") -> httpx.URL:
        """Один space: 404 на несуществующий ключ; expand раскрывает описание."""
        if not expand:
            return self._cub.url_of("space", space_key)

        return self._cub.url_of("space", space_key, params={"expand": expand})

    def space_content_path(
        self,
        space_key: str,
        *,
        content_type: ContentType = ContentType.PAGE,
        expand: str = DISCOVERY_EXPAND,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> httpx.URL:
        """Страницы или блог-записи спейса списком из базы, а не из поискового
        индекса.

        Поиск не отдаёт контент архивных спейсов и отстаёт от только что
        созданных страниц; этот список знает и то, и другое.
        """
        return self._cub.url_of(
            "space",
            space_key,
            "content",
            str(content_type),
            params={
                "limit": limit,
                "start": 0,
                "expand": expand,
            },
        )

    def space_list_path(
        self,
        space_type: SpaceType,
        *,
        expand: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> httpx.URL:
        """Формирует url запрос страниц которые расположены в space_key"""
        params: dict[str, object] = {"limit": limit, "start": 0}
        if space_type is not SpaceType.ANY:
            params["type"] = str(space_type)

        if expand:
            params["expand"] = expand

        return self._cub.url_of("space", params=params)

    def cql_search_path(
        self,
        cql: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        start: int = 0,
        expand: str | None = None,
    ) -> httpx.URL:
        params: dict[str, object] = {"cql": cql, "limit": limit, "start": start}
        if expand:
            params["expand"] = expand

        return self._cub.url_of("content", "search", params=params)

    def make_page_request(
        self,
        *,
        connection: HttpConnection,
        content: ConfluenceContent,
        body_format: str,
    ) -> ConfluenceRequest:
        path = self.page_body_path(content.id, body_format=body_format)
        meta = (
            Metadata.empty()
            .set(ConfluenceKeys.PAGE_ID, content.id)
            .set(ConfluenceKeys.HOST, connection.address_host())
        )
        return ConfluenceRequest(
            http=HttpRequest(url=str(path), method="GET"),
            mark=self._marks.page(content.version.number),
            metadata=meta,
        )

    def make_gone_request(
        self,
        *,
        connection: HttpConnection,
        page_id: str,
        body_format: str,
    ) -> ConfluenceRequest:
        """Запрос страницы, которой обход не нашёл: ответ 404 снимет её с индекса.

        Отметка версии заведомо не совпадёт с записью реестра, поэтому конвейер
        сходит за телом и получит от Confluence прямой ответ, есть страница
        или нет.
        """
        path = self.page_body_path(page_id, body_format=body_format)
        meta = (
            Metadata.empty()
            .set(ConfluenceKeys.PAGE_ID, page_id)
            .set(ConfluenceKeys.HOST, connection.address_host())
        )
        return ConfluenceRequest(
            http=HttpRequest(url=str(path), method="GET"),
            mark=self._marks.page(CflRestBuilder.UNKNOWN_VERSION),
            metadata=meta,
        )

    def make_attachment_request(  # noqa: PLR0913
        self,
        *,
        connection: HttpConnection,
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
            .set(ConfluenceKeys.HOST, connection.address_host())
        )
        if page.space.key:
            meta = meta.set(ConfluenceKeys.SPACE_KEY, page.space.key)

        if ancestors := page.ancestor_titles():
            meta = meta.set(ConfluenceKeys.ANCESTORS_TITLES, ancestors)

        if page.links.webui:
            parent_url = str(connection.url_of(page.links.webui))
            meta = meta.set(ConfluenceKeys.PARENT_URL, parent_url)

        att_path = attachment.download_path
        if attachment.webui:
            att_path = attachment.webui

        meta = meta.set(ConfluenceKeys.SOURCE_URL, str(connection.url_of(att_path)))
        return ConfluenceRequest(
            http=HttpRequest(url=attachment.download_path, method="GET"),
            mark=self._marks.attachment(
                attachment, parent=page_source, grade=grade, skip=skip
            ),
            metadata=meta,
        )


class CflRest:
    """GET по REST Confluence общим транспортом: байты, JSON-объект и обход
    пагинированной выдачи по `_links.next`. Транспорт (auth, ретраи, дамп)
    даёт вызывающий и сам закрывает.
    """

    def __init__(self, http: HttpTransport) -> None:
        self._http = http
        self._url_builder = CflUrlBuilder()

    async def get(self, url: httpx.URL) -> bytes:
        """Один GET, тело целиком: статус и обрыв уходят TransportError."""
        logger.info("confluence request: GET %s", url)
        elapsed = Elapsed()
        try:
            async with self._http.fetch(HttpRequest(url=str(url))) as resp:
                payload = await resp.stream.read()
        except httpx.HTTPStatusError as exc:
            msg = (
                f"GET {url} on confluence: expected 2xx, got "
                f"{exc.response.status_code} {exc.response.reason_phrase}"
            )
            raise TransportError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"GET {url} on confluence: {type(exc).__name__}: {exc}"
            raise TransportError(msg) from exc

        logger.info("confluence response: %d bytes in %dms", len(payload), elapsed.ms())

        return payload

    async def get_payload(self, url: httpx.URL) -> tuple[dict[str, Any], bytes]:
        """JSON-объект ответа вместе с сырыми байтами: от них считают отпечаток."""
        payload = await self.get(url)
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            msg = (
                f"GET {url} on confluence: expected JSON, got {payload[:200]!r}: {exc}"
            )
            raise ConfluencePayloadError(msg) from exc

        if not isinstance(data, dict):
            msg = (
                f"GET {url} on confluence: expected a JSON object, "
                f"got {type(data).__name__}"
            )
            raise ConfluencePayloadError(msg)

        return data, payload

    async def get_json(self, url: httpx.URL) -> dict[str, Any]:
        data, _ = await self.get_payload(url)

        return data

    async def pages(self, url: httpx.URL) -> AsyncIterator[dict[str, Any]]:
        """Элементы results всех страниц выдачи: сервер сам даёт следующее окно."""
        next_url: httpx.URL | None = url
        while next_url is not None:
            data = await self.get_json(next_url)
            node = JsonNode(data)
            results = node.results()
            next_url = self._next(node)
            logger.info(
                "confluence page: %d items, next=%s", len(results), next_url is not None
            )
            for raw in results:
                yield raw

    def _next(self, node: JsonNode) -> httpx.URL | None:
        link = node.next_link()
        if not link:
            return None

        return self._url_builder.raw_to_url(link)


class CflPaginator:
    """Модели из пагинированных discovery-запросов Confluence поверх CflRest;
    транспорт — CancellableHttpTransport по соединению, живёт до закрытия."""

    def __init__(self, conn: ConfluenceConnection):
        self._http = CancellableHttpTransport(conn.connection, conn.transport)
        self.rest = CflRest(self._http)

    async def __call__(self, url: httpx.URL, item: type[T]) -> AsyncIterator[T]:
        async for raw in self.rest.pages(url):
            yield self.item(item, raw, url)

    async def one(self, url: httpx.URL, item: type[T]) -> T:
        """Один объект вместо списка: содержимое ответа и есть результат."""
        data = await self.rest.get_json(url)

        return self.item(item, data, url)

    def item(self, item: type[T], raw: dict[str, Any], url: httpx.URL) -> T:
        try:
            return item.model_validate(raw)
        except ValidationError as exc:
            msg = (
                f"confluence discovery: GET {url} expected {item.__name__} items, "
                f"got {json.dumps(raw)[:200]}: {exc}"
            )
            raise ConfluencePayloadError(msg) from exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._http.close()
