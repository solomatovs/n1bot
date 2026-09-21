"""Доступ к Confluence Server REST: endpoint, адреса, запросы и пагинатор.

- ConfluenceConnection — endpoint: web-профиль (адрес, auth, ретраи), формат
  тела и дамп обмена.
- ConfluenceUrl        — сборка относительных адресов на httpx.URL.
- ConfluenceRest       — адреса запросов страниц, вложений, спейсов и поиска;
  конструкторы ConfluenceRequest для конвейера индексации.
- ConfluencePaginator  — клиент пагинированных discovery-запросов поверх
  HttpTransport проекта: auth, ретраи и дамп берутся из профиля.

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

from boba.chat.http import HttpDumpConfig
from boba.confluence.models import (
    AttachmentInfo,
    ConfluenceContent,
    ConfluenceKeys,
    ConfluenceMarks,
    ConfluencePayloadError,
    ParseGrade,
)
from boba.confluence.parsing import ConfluenceJson
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
from boba.transport.http import CancellableHttpTransport, HttpRequest
from boba.transport.http.profile import HttpConnection

__all__ = [
    "ConfluenceConnection",
    "ConfluencePaginator",
    "ConfluenceRequest",
    "ConfluenceRest",
    "ConfluenceUrl",
    "ContentType",
    "SpaceStatus",
    "SpaceType",
]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class ConfluenceConnection(BaseModel):
    """Confluence endpoint: формат тела, транспортный профиль и дамп обмена."""

    body_format: Literal["view", "export_view", "storage"] = Field(
        default="view",
        description=(
            "`view` — clean HTML (рекомендуется); `export_view` — с макросами; "
            "`storage` — raw storage XML."
        ),
    )
    profile: HttpConnection = Field(
        description=(
            "Транспортный web-профиль (host/port/path/timeout/ssl/auth) ссылкой "
            '`profile = "${web.<name>}"`. Адрес Confluence задаётся в профиле; '
            "auth (PAT/Basic) — там же `auth = { method = 'bearer', token = '...' }`."
        ),
    )
    dump: HttpDumpConfig = Field(
        default_factory=HttpDumpConfig,
        description="Дамп HTTP-обмена с Confluence в файлы по хосту.",
    )


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
        expand = f"body.{body_format},version,ancestors,space,metadata.labels,history"
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
    def comments_path(
        page_id: str,
        *,
        expand: str,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> httpx.URL:
        """Комментарии страницы с телами: у страницы нет признака, что они
        менялись, поэтому список читается на каждом обходе."""
        return ConfluenceUrl.of(
            "content",
            page_id,
            "child",
            "comment",
            params={"limit": limit, "start": 0, "expand": expand},
        )

    @staticmethod
    def space_path(space_key: str, *, expand: str = "") -> httpx.URL:
        """Один space: 404 на несуществующий ключ; expand раскрывает описание."""
        if not expand:
            return ConfluenceUrl.of("space", space_key)

        return ConfluenceUrl.of("space", space_key, params={"expand": expand})

    @staticmethod
    def space_content_path(
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
        return ConfluenceUrl.of(
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

    @staticmethod
    def space_list_path(
        space_type: SpaceType,
        *,
        expand: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> httpx.URL:
        params: dict[str, object] = {"limit": limit, "start": 0}
        if space_type is not SpaceType.ANY:
            params["type"] = str(space_type)

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


class ConfluencePaginator:
    """httpx-клиент для пагинированных Confluence REST discovery-запросов.

    Исполнение и retry (5xx/transport) — внутри HttpTransport, собранного из
    conn.profile; пагинатор лишь строит path, ходит по `_links.next` и
    разбирает результаты в модель item.
    """

    def __init__(self, conn: ConfluenceConnection):
        self._http = CancellableHttpTransport(conn.profile, dump=conn.dump)

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
