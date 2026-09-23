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
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Self

import httpx
from markdownify import MarkdownConverter
from pydantic import BaseModel, ConfigDict, Field

from boba.confluence.html import ConfluenceHtml
from boba.confluence.models import ConfluenceSpaceItem, PageLink, SpaceMask
from boba.confluence.rest import (
    CflRestBuilder,
    ConfluenceConnection,
    ContentType,
    SpaceStatus,
    SpaceType,
)
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


def hash_sum(original: bytes) -> str:
    return hashlib.sha256(original).hexdigest()


def render_markdown(
    html: str, *, page_id: str, title: str
) -> tuple[str, tuple[PageLink, ...]]:
    """Markdown и ссылки на другие страницы из одного дерева html."""
    soup = ConfluenceHtml.parse_html(html)
    links = ConfluenceHtml.collect_targets(soup, page_id=page_id, title=title)
    converter = MarkdownConverter(
        heading_style="ATX", escape_underscores=False, escape_asterisks=False
    )
    markdown = str(converter.convert_soup(soup)).strip()
    soup.decompose()

    return markdown, links


def parse_space(raw: dict[str, Any], content_hash: str) -> Space:
    key = read_str(raw, "key")
    name = read_str(raw, "name")
    space_type = read_str(raw, "type")
    status = read_str(raw, "status")
    description = read_str(raw, "description", "plain", "value")

    # есть вопросики к тому, почему выбран такой хэщ от space страницы
    # content_hash = hash_text(f"{name}\n{description}")

    if not name:
        name = key

    return Space(
        key=key,
        name=name,
        space_type=space_type,
        status=status,
        description=description,
        content_hash=content_hash,
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

    GONE_NUMBER: ClassVar[int] = 404

    def __init__(self, conn: ConfluenceConnection) -> None:
        self._conn = conn
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

    async def fetch_json(self, url: httpx.URL) -> tuple[dict[str, Any], str]:
        try:
            async with self._http.fetch(HttpRequest(url=str(url))) as resp:
                # здесь вычитывается вся страница в память
                # что не очень хорошо для потоковой обработки
                # однако одна страница в памяти удобней чем
                # геморой потоковой обработки страниц конфлюенса
                payload = await resp.stream.read()
                payload_hash = hash_sum(payload)
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

        return data, payload_hash

    async def iter_page_urls(self, url: httpx.URL) -> AsyncIterator[dict[str, Any]]:
        """
        Достает из confluence url адреса страниц
        без контента страниц, только url адреса
        """
        next_url: httpx.URL | None = url
        while next_url is not None:
            data, _ = await self.fetch_json(next_url)

            link = read_str(data, "_links", "next")
            next_url = None
            if link:
                # confluence сам возвращает следующую страницу для запроса
                # согласно тому, что ты передал
                # к примеру если запрос страниц был: start=0&limit=50
                # то следующая страница будет с такими же параметрами
                # но следующим окном: start=50&limit=50
                # запоминаем этот url
                next_url = httpx.URL(link)

            for item in read_results(data):
                # елдим результат для постраничной обработки
                # каждая страница запрашивается, парситься, сохраняется последовательно
                # друг за другом, без необходимости все страницы читать в память
                yield item

    async def list_space_keys(self, selector: SpaceSelector) -> AsyncIterator[str]:
        """Ключи как есть или обход списка сервера по маскам."""
        mask = SpaceMask.of_masks(selector.masks)
        if not mask.has_wildcard:
            for x in list(mask.keys()):
                yield x

            return

        it = self.iter_page_urls(url=self._crb.space_list_path(selector.type))
        async for space in it:
            key = read_str(space, "key")
            if not key:
                continue

            archived = read_str(space, "status") == SpaceStatus.ARCHIVED
            if archived and not selector.archived:
                continue

            if mask.matches(ConfluenceSpaceItem.model_validate(space)):
                yield key


    async def read_space(self, key: str) -> Space:
        url = self._crb.space_path(key, expand="description.plain")
        space_dict, space_hash = await self.fetch_json(url)

        return parse_space(space_dict, space_hash)

    async def iter_contents(
        self, key: str, kind: ContentType
    ) -> AsyncIterator[Content]:
        url = self._crb.space_content_path(
            key,
            content_type=kind,
            expand=(
                "version,space,ancestors,metadata.labels,history,"
                "children.attachment.version,children.attachment.extensions"
            ),
        )
        async for raw in self.iter_page_urls(url):
            yield parse_content(raw, kind)

    async def read_body(self, content: Content) -> Content:
        """Та же запись с markdown, хэшем оригинала и ссылками; html не хранится."""
        body_format = self._conn.body_format
        url = self._crb.page_body_path(content.id, body_format=body_format)
        raw, _ = await self.fetch_json(url)
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

        url = self._crb.attachments_path(content.id)

        async for raw in self.iter_page_urls(url):
            yield parse_attachment(
                raw,
                page_id=content.id,
                space_key=content.space_key,
                where=f"{content.kind} {content.id}",
            )

    async def iter_comments(self, content: Content) -> AsyncIterator[Comment]:
        """С телами; признака смены комментариев у страницы нет, читаются всегда."""
        body_format = self._conn.body_format
        expand = f"body.{body_format},version,history,extensions.location,container"
        url = self._crb.comments_path(content.id, expand=expand)

        async for raw in self.iter_page_urls(url):
            yield parse_comment(raw, content, body_format)

    async def download_attachment(self, attachment: Attachment, into: Path) -> str:
        """Файл на диск чанками; возвращает sha256 байтов."""
        where = f"attachment {attachment.id} {attachment.title!r}"
        request = HttpRequest(url=attachment.download_path)
        digest = hashlib.sha256()

        try:
            async with self._http.fetch(request) as resp:
                with into.open("wb") as file:
                    async for chunk in resp.stream:
                        digest.update(chunk)
                        file.write(chunk)
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

        return digest.hexdigest()
