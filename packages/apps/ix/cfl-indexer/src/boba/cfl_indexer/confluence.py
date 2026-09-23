"""Чтение Confluence для обхода спейса: ключи спейсов по маскам, списки страниц без
тел, тело страницы в markdown, вложения и их файлы, комментарии. Ответ сервера живёт
как dict от json.loads до разбора в запись; файл вложения идёт через пипу в ридер.
version — номер версии из списка Confluence: по нему обход решает, запрашивать ли тело.
content_hash — sha256 сырого контента сущности, как его отдал сервер: у страницы и
спейса байты ответа с телом, у вложения байты файла, у комментария html его тела.

Ошибки:
ConfluenceReadError — транспорт, статус, форма ответа или его поля.
AttachmentGoneError — вложение снято между списком и скачиванием (404).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterable, AsyncIterator, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, BinaryIO, ClassVar, Self, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.confluence.html import ConfluencePage, PageMarkdown
from boba.confluence.models import ConfluenceSpaceItem, PageLink, SpaceMask
from boba.confluence.parsing import BodyHasher, JsonNode, RunningDigest
from boba.confluence.rest import (
    CflRestBuilder,
    ConfluenceConnection,
    ContentType,
    SpaceStatus,
    SpaceType,
)
from boba.doc.bridge import AsyncPipe
from boba.transport.http import CancellableHttpTransport, HttpRequest

__all__ = [
    "Attachment",
    "AttachmentGoneError",
    "Comment",
    "ConfluenceParser",
    "ConfluenceReadError",
    "ConfluenceReader",
    "Content",
    "Space",
    "SpaceSelector",
]

logger = logging.getLogger("cfl-indexer")

T = TypeVar("T")


class ConfluenceReadError(Exception):
    """Confluence не прочитан: транспорт, статус, форма ответа или его поля."""


class AttachmentGoneError(ConfluenceReadError):
    """Вложение снято между списком и скачиванием."""


class SpaceSelector(BaseModel):
    """Маски ключей спейсов, их вид и брать ли архивные."""

    model_config = ConfigDict(frozen=True)

    masks: Sequence[str] = Field(min_length=1)
    type: SpaceType
    archived: bool


@dataclass(frozen=True, slots=True)
class Space:
    key: str
    name: str
    space_type: str
    status: str
    description: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class Attachment:
    id: str
    page_id: str
    space_key: str
    title: str
    media_type: str
    file_size: int
    version: int
    download_path: str
    updated_at: datetime
    author: str


@dataclass(frozen=True, slots=True)
class Content:
    """Страница или блог-запись; markdown, хэш и ссылки появляются после read_body."""

    id: str
    kind: ContentType
    space_key: str
    title: str
    status: str
    version: int
    parent_id: str
    ancestor_titles: tuple[str, ...]
    labels: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    author: str
    last_editor: str
    attachments: tuple[Attachment, ...] = ()
    attachments_truncated: bool = False
    content_hash: str = ""
    markdown: str = ""
    links: tuple[PageLink, ...] = ()


@dataclass(frozen=True, slots=True)
class Comment:
    id: str
    page_id: str
    space_key: str
    location: str
    version: int
    created_at: datetime
    updated_at: datetime
    author: str
    content_hash: str
    markdown: str


@dataclass(frozen=True)
class Payload:
    """Один ответ REST: разобранный JSON и сырые байты, от которых считается
    отпечаток сущности."""

    data: dict[str, Any]
    raw: bytes


class ConfluenceParser:
    """Сырой JSON Confluence в записи обхода: спейс, страница, вложение,
    комментарий; тела страниц — в markdown, хэш от оригинального html.

    Создаётся ридером под формат тела соединения; конвертер markdown один на
    обход, ссылки на другие страницы снимаются с того же дерева html.
    """

    HEADING_STYLE: ClassVar[str] = "ATX"

    def __init__(self, body_format: str) -> None:
        self._body_format = body_format
        self._markdown = PageMarkdown(self.HEADING_STYLE, escape=False)
        self._hasher = BodyHasher()

    @property
    def body_format(self) -> str:
        return self._body_format

    def space(self, payload: Payload) -> Space:
        """Запись спейса; content_hash — от сырых байт ответа сервера."""
        node = JsonNode(payload.data)
        key = node.str("key")
        name = node.str("name")
        if not name:
            name = key

        return Space(
            key=key,
            name=name,
            space_type=node.str("type"),
            status=node.str("status"),
            description=node.str("description", "plain", "value"),
            content_hash=self._hasher.hexdigest(payload.raw),
        )

    def content(self, raw: dict[str, Any], kind: ContentType) -> Content:
        node = JsonNode(raw)
        content_id = node.str("id")
        space_key = node.str("space", "key")
        where = f"{kind} {content_id}"
        ancestors = node.list("ancestors")
        parent_id = ""
        if ancestors:
            parent_id = JsonNode(ancestors[-1]).str("id")

        block = JsonNode(node.dict("children", "attachment"))
        limit = block.int("limit")
        attachments: list[Attachment] = []
        for item in block.list("results"):
            attachments.append(
                self.attachment(
                    item, page_id=content_id, space_key=space_key, where=where
                )
            )

        return Content(
            id=content_id,
            kind=kind,
            space_key=space_key,
            title=node.str("title"),
            status=node.str("status"),
            version=node.int("version", "number"),
            parent_id=parent_id,
            ancestor_titles=node.ancestor_titles(),
            labels=self.labels(node.list("metadata", "labels", "results")),
            created_at=self.stamp(
                node.str("history", "createdDate"), f"{where}: createdDate"
            ),
            updated_at=self.stamp(node.str("version", "when"), f"{where}: when"),
            author=self.user(node.dict("history", "createdBy")),
            last_editor=self.user(node.dict("version", "by")),
            attachments=tuple(attachments),
            attachments_truncated=limit > 0 and block.int("size") >= limit,
        )

    def attachment(
        self, raw: dict[str, Any], *, page_id: str, space_key: str, where: str
    ) -> Attachment:
        node = JsonNode(raw)
        attachment_id = node.str("id")
        where = f"attachment {attachment_id} of {where}"

        return Attachment(
            id=attachment_id,
            page_id=page_id,
            space_key=space_key,
            title=node.str("title"),
            media_type=node.str("extensions", "mediaType"),
            file_size=node.int("extensions", "fileSize"),
            version=node.int("version", "number"),
            download_path=node.str("_links", "download"),
            updated_at=self.stamp(node.str("version", "when"), f"{where}: when"),
            author=self.user(node.dict("version", "by")),
        )

    def comment(self, raw: dict[str, Any], page: Content) -> Comment:
        node = JsonNode(raw)
        comment_id = node.str("id")
        where = f"comment {comment_id} of {page.kind} {page.id}"
        html = node.body_html(self._body_format)
        markdown, _ = self.markdown(html, page_id=page.id, title=page.title)

        return Comment(
            id=comment_id,
            page_id=page.id,
            space_key=page.space_key,
            location=node.str("extensions", "location"),
            version=node.int("version", "number"),
            created_at=self.stamp(
                node.str("history", "createdDate"), f"{where}: createdDate"
            ),
            updated_at=self.stamp(node.str("version", "when"), f"{where}: when"),
            author=self.user(node.dict("history", "createdBy")),
            content_hash=self.text_hash(html),
            markdown=markdown,
        )

    def body(self, payload: Payload, content: Content) -> Content:
        """Та же запись с markdown и ссылками; content_hash — от сырых байт
        ответа с телом, html не хранится."""
        html = JsonNode(payload.data).body_html(self._body_format)
        fresh = self.content(payload.data, content.kind)
        markdown, links = self.markdown(html, page_id=content.id, title=content.title)

        return replace(
            fresh,
            content_hash=self._hasher.hexdigest(payload.raw),
            markdown=markdown,
            links=links,
        )

    def markdown(
        self, html: str, *, page_id: str, title: str
    ) -> tuple[str, tuple[PageLink, ...]]:
        """Markdown и ссылки на другие страницы из одного дерева html."""
        page = ConfluencePage(html, page_id=page_id, title=title)
        try:
            links = page.targets()
            markdown = self._markdown.render(page)
        finally:
            page.close()

        return markdown, links

    def stamp(self, raw: str, where: str) -> datetime:
        if not raw:
            raise ConfluenceReadError(
                f"confluence {where}: expected a timestamp, got empty"
            )

        try:
            return datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ConfluenceReadError(
                f"confluence {where}: expected an ISO timestamp, got {raw!r}: {exc}"
            ) from exc

    def user(self, data: dict[str, Any]) -> str:
        node = JsonNode(data)
        username = node.str("username")
        if username:
            return username

        return node.str("displayName")

    def labels(self, labels: list[Any]) -> tuple[str, ...]:
        names: list[str] = []
        for label in labels:
            name = JsonNode(label).str("name").strip()
            if name and name not in names:
                names.append(name)

        return tuple(names)

    def text_hash(self, original: str) -> str:
        return self._hasher.text(original)


class ConfluenceReader:
    """Запросы одного обхода: auth, ретраи и дамп из ConfluenceConnection,
    выбор спейсов по маскам источника."""

    GONE_NUMBER: ClassVar[int] = 404

    def __init__(
        self, conn: ConfluenceConnection, spaces: SpaceSelector, list_limit: int
    ) -> None:
        self._conn = conn
        self._spaces = spaces
        self._list_limit = list_limit
        self._mask = SpaceMask(spaces.masks)
        self._parser = ConfluenceParser(conn.body_format)
        self._hasher = BodyHasher()
        self._http = CancellableHttpTransport(conn.profile, dump=conn.dump)
        self._crb = CflRestBuilder()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.close()

    async def fetch(self, url: httpx.URL):
        try:
            async with self._http.fetch(HttpRequest(url=str(url))) as resp:
                yield resp
        except httpx.HTTPError as exc:
            raise ConfluenceReadError(
                f"GET {url}: {type(exc).__name__}: {exc}"
            ) from exc

    async def fetch_payload(self, url: httpx.URL) -> Payload:
        """Один ответ целиком в память: JSON иначе не разобрать, а объём
        ограничен одним ответом сервера, не спейсом."""
        try:
            async with self._http.fetch(HttpRequest(url=str(url))) as resp:
                payload = await resp.stream.read()
        except httpx.HTTPError as exc:
            raise ConfluenceReadError(
                f"GET {url}: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ConfluenceReadError(
                f"GET {url}: expected JSON, got {payload[:120]!r}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ConfluenceReadError(
                f"GET {url}: expected an object, got {type(data).__name__}"
            )

        return Payload(data=data, raw=payload)

    async def iter_page_urls(self, url: httpx.URL) -> AsyncIterator[dict[str, Any]]:
        """
        Достает из confluence url адреса страниц
        без контента страниц, только url адреса
        """
        next_url: httpx.URL | None = url
        while next_url is not None:
            payload = await self.fetch_payload(next_url)

            node = JsonNode(payload.data)
            link = node.next_link()
            next_url = None
            if link:
                # confluence сам возвращает следующую страницу для запроса
                # согласно тому, что ты передал
                # к примеру если запрос страниц был: start=0&limit=50
                # то следующая страница будет с такими же параметрами
                # но следующим окном: start=50&limit=50
                # запоминаем этот url
                next_url = httpx.URL(link)

            for item in node.results():
                # елдим результат для постраничной обработки
                # каждая страница запрашивается, парситься, сохраняется последовательно
                # друг за другом, без необходимости все страницы читать в память
                yield item

    async def list_space_keys(self) -> AsyncIterator[str]:
        """Ключи как есть или обход списка сервера по маскам источника."""
        if not self._mask.has_wildcard:
            for key in self._mask.as_keys():
                yield key

            return

        it = self.iter_page_urls(url=self._crb.space_list_path(self._spaces.type))
        async for space in it:
            node = JsonNode(space)
            key = node.str("key")
            if not key:
                continue

            archived = node.str("status") == SpaceStatus.ARCHIVED
            if archived and not self._spaces.archived:
                continue

            if self._mask.matches(ConfluenceSpaceItem.model_validate(space)):
                yield key

    async def read_space(self, key: str) -> Space:
        url = self._crb.space_path(key, expand="description.plain")
        return self._parser.space(await self.fetch_payload(url))

    async def iter_contents(
        self, key: str, kind: ContentType
    ) -> AsyncIterator[Content]:
        url = self._crb.space_content_path(
            key,
            content_type=kind,
            limit=self._list_limit,
            expand=(
                "version,space,ancestors,metadata.labels,history,"
                "children.attachment.version,children.attachment.extensions"
            ),
        )
        async for raw in self.iter_page_urls(url):
            yield self._parser.content(raw, kind)

    async def read_body(self, content: Content) -> Content:
        """Та же запись с markdown, хэшем оригинала и ссылками; html не хранится."""
        url = self._crb.page_body_path(content.id, body_format=self._parser.body_format)
        return self._parser.body(await self.fetch_payload(url), content)

    async def iter_attachments(self, content: Content) -> AsyncIterator[Attachment]:
        """Из раскрытия списка, при усечении — полным списком."""
        if not content.attachments_truncated:
            for item in content.attachments:
                yield item

            return

        url = self._crb.attachments_path(content.id, limit=self._list_limit)

        async for raw in self.iter_page_urls(url):
            yield self._parser.attachment(
                raw,
                page_id=content.id,
                space_key=content.space_key,
                where=f"{content.kind} {content.id}",
            )

    async def iter_comments(self, content: Content) -> AsyncIterator[Comment]:
        """С телами; признака смены комментариев у страницы нет, читаются всегда."""
        body_format = self._parser.body_format
        expand = f"body.{body_format},version,history,extensions.location,container"
        url = self._crb.comments_path(content.id, expand=expand, limit=self._list_limit)

        async for raw in self.iter_page_urls(url):
            yield self._parser.comment(raw, content)

    async def read_attachment(
        self, attachment: Attachment, consume: Callable[[BinaryIO], T]
    ) -> tuple[str, T]:
        """Тело файла из http сразу в потребителя через пипу, без файла на
        диске; sha256 байтов считается по дороге и возвращается с итогом."""
        where = f"attachment {attachment.id} {attachment.title!r}"
        request = HttpRequest(url=attachment.download_path)
        digest = self._hasher.stream()

        try:
            async with self._http.fetch(request) as resp:
                result = await AsyncPipe.run(self._hashed(resp.stream, digest), consume)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            msg = (
                f"confluence {where}: GET {attachment.download_path} "
                f"expected 2xx, got {status}"
            )
            if status == self.GONE_NUMBER:
                raise AttachmentGoneError(msg) from exc

            raise ConfluenceReadError(msg) from exc
        except httpx.HTTPError as exc:
            raise ConfluenceReadError(
                f"confluence {where}: GET {attachment.download_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        return digest.hexdigest(), result

    async def _hashed(
        self, chunks: AsyncIterable[bytes], digest: RunningDigest
    ) -> AsyncIterator[bytes]:
        async for chunk in chunks:
            digest.update(chunk)
            yield chunk
