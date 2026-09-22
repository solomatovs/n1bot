"""Чтение Confluence для обхода спейса: ключи спейсов по маскам, списки страниц без
тел, тело страницы в markdown, вложения и их файлы, комментарии. Ответ сервера живёт
как dict от json.loads до разбора в запись; файл вложения идёт на диск чанками.
content_hash считается от оригинала, в индекс идёт преобразование.

Ошибки:
ConfluenceReadError — транспорт, статус, форма ответа или его поля.
AttachmentGoneError — вложение снято между списком и скачиванием (404).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterable, AsyncIterator, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, BinaryIO, Self, TypeVar

import httpx
from markdownify import MarkdownConverter
from pydantic import BaseModel, ConfigDict, Field

from boba.confluence.html import ConfluenceHtml
from boba.confluence.models import ConfluenceSpaceItem, PageLink, SpaceMask
from boba.confluence.rest import (
    ConfluenceConnection,
    ConfluenceRest,
    ContentType,
    SpaceStatus,
    SpaceType,
)
from boba.doc import AsyncPipe
from boba.transport.http import CancellableHttpTransport, HttpRequest

__all__ = [
    "Attachment",
    "AttachmentGoneError",
    "Comment",
    "ConfluenceReadError",
    "ConfluenceReader",
    "Content",
    "Space",
    "SpaceSelector",
    "hash_text",
    "render_markdown",
]

logger = logging.getLogger("cfl-indexer")

HEADING_STYLE = "ATX"
SPACE_EXPAND = "description.plain"
LIST_EXPAND = (
    "version,space,ancestors,metadata.labels,history,"
    "children.attachment.version,children.attachment.extensions"
)
COMMENT_EXPAND = "body.{body_format},version,history,extensions.location,container"
GONE_STATUS = 404
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


def read_dict(data: Any, *path: str) -> dict[str, Any]:
    for key in path:
        if not isinstance(data, dict):
            return {}

        data = data.get(key)

    if not isinstance(data, dict):
        return {}

    return data


def read_str(data: Any, *path: str) -> str:
    value = read_dict(data, *path[:-1]).get(path[-1])
    if value is None:
        return ""

    return str(value)


def read_int(data: Any, *path: str) -> int:
    value = read_dict(data, *path[:-1]).get(path[-1])
    if value is None:
        return 0

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def read_list(data: Any, *path: str) -> list[Any]:
    value = read_dict(data, *path[:-1]).get(path[-1])
    if not isinstance(value, list):
        return []

    return value


def read_results(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = data.get("results")
    if not isinstance(items, list):
        items = read_list(data, "page", "results")

    found: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            found.append(item)

    return found


def parse_stamp(raw: str, where: str) -> datetime:
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


def parse_user(data: Any) -> str:
    username = read_str(data, "username")
    if username:
        return username

    return read_str(data, "displayName")


def parse_titles(ancestors: list[Any]) -> tuple[str, ...]:
    titles: list[str] = []
    for ancestor in ancestors:
        title = read_str(ancestor, "title").strip()
        if title:
            titles.append(title)

    return tuple(titles)


def parse_labels(labels: list[Any]) -> tuple[str, ...]:
    names: list[str] = []
    for label in labels:
        name = read_str(label, "name").strip()
        if name and name not in names:
            names.append(name)

    return tuple(names)


def hash_text(original: str) -> str:
    return hashlib.sha256(original.encode("utf-8")).hexdigest()


def render_markdown(
    html: str, *, page_id: str, title: str
) -> tuple[str, tuple[PageLink, ...]]:
    """Markdown и ссылки на другие страницы из одного дерева html."""
    soup = ConfluenceHtml.parse_html(html)
    links = ConfluenceHtml.collect_targets(soup, page_id=page_id, title=title)
    converter = MarkdownConverter(
        heading_style=HEADING_STYLE, escape_underscores=False, escape_asterisks=False
    )
    markdown = str(converter.convert_soup(soup)).strip()
    soup.decompose()

    return markdown, links


def parse_space(raw: dict[str, Any]) -> Space:
    key = read_str(raw, "key")
    name = read_str(raw, "name")
    if not name:
        name = key

    description = read_str(raw, "description", "plain", "value")

    return Space(
        key=key,
        name=name,
        space_type=read_str(raw, "type"),
        status=read_str(raw, "status"),
        description=description,
        content_hash=hash_text(f"{name}\n{description}"),
    )


def parse_attachment(
    raw: dict[str, Any], *, page_id: str, space_key: str, where: str
) -> Attachment:
    attachment_id = read_str(raw, "id")
    where = f"attachment {attachment_id} of {where}"

    return Attachment(
        id=attachment_id,
        page_id=page_id,
        space_key=space_key,
        title=read_str(raw, "title"),
        media_type=read_str(raw, "extensions", "mediaType"),
        file_size=read_int(raw, "extensions", "fileSize"),
        version=read_int(raw, "version", "number"),
        download_path=read_str(raw, "_links", "download"),
        updated_at=parse_stamp(read_str(raw, "version", "when"), f"{where}: when"),
        author=parse_user(read_dict(raw, "version", "by")),
    )


def parse_content(raw: dict[str, Any], kind: ContentType) -> Content:
    content_id = read_str(raw, "id")
    space_key = read_str(raw, "space", "key")
    where = f"{kind} {content_id}"
    ancestors = read_list(raw, "ancestors")
    parent_id = ""
    if ancestors:
        parent_id = read_str(ancestors[-1], "id")

    block = read_dict(raw, "children", "attachment")
    limit = read_int(block, "limit")
    attachments: list[Attachment] = []
    for item in read_list(block, "results"):
        attachments.append(
            parse_attachment(item, page_id=content_id, space_key=space_key, where=where)
        )

    return Content(
        id=content_id,
        kind=kind,
        space_key=space_key,
        title=read_str(raw, "title"),
        status=read_str(raw, "status"),
        version=read_int(raw, "version", "number"),
        parent_id=parent_id,
        ancestor_titles=parse_titles(ancestors),
        labels=parse_labels(read_list(raw, "metadata", "labels", "results")),
        created_at=parse_stamp(
            read_str(raw, "history", "createdDate"), f"{where}: createdDate"
        ),
        updated_at=parse_stamp(read_str(raw, "version", "when"), f"{where}: when"),
        author=parse_user(read_dict(raw, "history", "createdBy")),
        last_editor=parse_user(read_dict(raw, "version", "by")),
        attachments=tuple(attachments),
        attachments_truncated=limit > 0 and read_int(block, "size") >= limit,
    )


def parse_comment(raw: dict[str, Any], page: Content, body_format: str) -> Comment:
    comment_id = read_str(raw, "id")
    where = f"comment {comment_id} of {page.kind} {page.id}"
    html = read_str(raw, "body", body_format, "value")
    markdown, _ = render_markdown(html, page_id=page.id, title=page.title)

    return Comment(
        id=comment_id,
        page_id=page.id,
        space_key=page.space_key,
        location=read_str(raw, "extensions", "location"),
        version=read_int(raw, "version", "number"),
        created_at=parse_stamp(
            read_str(raw, "history", "createdDate"), f"{where}: createdDate"
        ),
        updated_at=parse_stamp(read_str(raw, "version", "when"), f"{where}: when"),
        author=parse_user(read_dict(raw, "history", "createdBy")),
        content_hash=hash_text(html),
        markdown=markdown,
    )


class ConfluenceReader:
    """Запросы одного обхода: auth, ретраи и дамп из ConfluenceConnection."""

    def __init__(self, conn: ConfluenceConnection) -> None:
        self._conn = conn
        self._http = CancellableHttpTransport(conn.profile, dump=conn.dump)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.close()

    async def fetch_json(self, url: httpx.URL, where: str) -> dict[str, Any]:
        try:
            async with self._http.fetch(HttpRequest(url=str(url))) as resp:
                payload = await resp.stream.read()
        except httpx.HTTPError as exc:
            raise ConfluenceReadError(
                f"confluence {where}: GET {url}: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ConfluenceReadError(
                f"confluence {where}: GET {url}: expected JSON, got "
                f"{payload[:120]!r}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ConfluenceReadError(
                f"confluence {where}: GET {url}: expected an object, got "
                f"{type(data).__name__}"
            )

        return data

    async def iter_pages(
        self, url: httpx.URL, where: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Объекты списка по одному; страница списка отпускается до следующей."""
        next_url: httpx.URL | None = url
        while next_url is not None:
            data = await self.fetch_json(next_url, where)
            items = read_results(data)
            link = read_str(data, "_links", "next")
            next_url = None
            if link:
                next_url = httpx.URL(link)

            while items:
                yield items.pop(0)

    async def list_space_keys(self, selector: SpaceSelector) -> list[str]:
        """Ключи как есть или обход списка сервера по маскам."""
        mask = SpaceMask.of_masks(selector.masks)
        if not mask.has_wildcard:
            return list(mask.keys())

        url = ConfluenceRest.space_list_path(selector.type)
        keys: list[str] = []
        async for raw in self.iter_pages(url, "space list"):
            key = read_str(raw, "key")
            if not key:
                continue

            archived = read_str(raw, "status") == SpaceStatus.ARCHIVED
            if archived and not selector.archived:
                continue

            if mask.matches(ConfluenceSpaceItem.model_validate(raw)):
                keys.append(key)

        return keys

    async def read_space(self, key: str) -> Space:
        url = ConfluenceRest.space_path(key, expand=SPACE_EXPAND)

        return parse_space(await self.fetch_json(url, f"space {key}"))

    async def iter_contents(
        self, key: str, kind: ContentType
    ) -> AsyncIterator[Content]:
        url = ConfluenceRest.space_content_path(
            key, content_type=kind, expand=LIST_EXPAND
        )
        async for raw in self.iter_pages(url, f"{kind} list of {key}"):
            yield parse_content(raw, kind)

    async def read_body(self, content: Content) -> Content:
        """Та же запись с markdown, хэшем оригинала и ссылками; html не хранится."""
        body_format = self._conn.body_format
        url = ConfluenceRest.page_body_path(content.id, body_format=body_format)
        raw = await self.fetch_json(url, f"{content.kind} {content.id} body")
        html = read_str(raw, "body", body_format, "value")
        fresh = parse_content(raw, content.kind)
        markdown, links = render_markdown(html, page_id=content.id, title=content.title)

        return replace(
            fresh, content_hash=hash_text(html), markdown=markdown, links=links
        )

    async def iter_attachments(self, content: Content) -> AsyncIterator[Attachment]:
        """Из раскрытия списка, при усечении — полным списком."""
        if not content.attachments_truncated:
            for item in content.attachments:
                yield item

            return

        url = ConfluenceRest.attachments_path(content.id)
        where = f"attachments of {content.kind} {content.id}"
        async for raw in self.iter_pages(url, where):
            yield parse_attachment(
                raw,
                page_id=content.id,
                space_key=content.space_key,
                where=f"{content.kind} {content.id}",
            )

    async def iter_comments(self, content: Content) -> AsyncIterator[Comment]:
        """С телами; признака смены комментариев у страницы нет, читаются всегда."""
        body_format = self._conn.body_format
        expand = COMMENT_EXPAND.format(body_format=body_format)
        url = ConfluenceRest.comments_path(content.id, expand=expand)
        where = f"comments of {content.kind} {content.id}"
        async for raw in self.iter_pages(url, where):
            yield parse_comment(raw, content, body_format)

    async def read_attachment(
        self, attachment: Attachment, consume: Callable[[BinaryIO], T]
    ) -> tuple[str, T]:
        """Тело файла из http сразу в потребителя через пипу, без файла на
        диске; sha256 байтов считается по дороге и возвращается с итогом."""
        where = f"attachment {attachment.id} {attachment.title!r}"
        request = HttpRequest(url=attachment.download_path)
        digest = hashlib.sha256()

        try:
            async with self._http.fetch(request) as resp:
                result = await AsyncPipe.run(self._hashed(resp.stream, digest), consume)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            msg = (
                f"confluence {where}: GET {attachment.download_path} "
                f"expected 2xx, got {status}"
            )
            if status == GONE_STATUS:
                raise AttachmentGoneError(msg) from exc

            raise ConfluenceReadError(msg) from exc
        except httpx.HTTPError as exc:
            raise ConfluenceReadError(
                f"confluence {where}: GET {attachment.download_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        return digest.hexdigest(), result

    @staticmethod
    async def _hashed(
        chunks: AsyncIterable[bytes], digest: hashlib._Hash
    ) -> AsyncIterator[bytes]:
        async for chunk in chunks:
            digest.update(chunk)
            yield chunk
