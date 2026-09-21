"""Чтение Confluence для индексатора: выбор спейсов по маскам, спейс, списки страниц
и блог-записей без тел, тело контента и его преобразование в текст индекса, список и
скачивание вложений, комментарии страницы с телами.

Правило текстовых аспектов: хэш от оригинала, индекс от преобразования. content_hash
считается от исходного HTML body.view как он пришёл (у вложения — от байтов файла,
sha256 считается по дороге на диск), а в индекс идёт markdown из markdownify или
текст, извлечённый из файла; сам оригинал после этого выбрасывается.

Ошибки:
ConfluenceReadError — Confluence недоступен, ответил статусом, отдал не тот JSON
    или в ответе нет обязательного поля (версия, даты).
AttachmentGoneError — вложение из списка уже снято: скачивание ответило 404.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.confluence.html import ConfluenceHtml, PageOps
from boba.confluence.models import (
    ConfluenceAttachmentItem,
    ConfluenceContent,
    ConfluencePayloadError,
    ConfluenceSpaceItem,
    PageLink,
    SpaceMask,
)
from boba.confluence.rest import (
    ConfluenceConnection,
    ConfluencePaginator,
    ConfluenceRest,
    ContentType,
    SpaceStatus,
    SpaceType,
)
from boba.indexing import TransportError
from boba.transport.http import CancellableHttpTransport, HttpRequest

__all__ = [
    "AttachmentGoneError",
    "AttachmentSummary",
    "CommentDocument",
    "ConfluenceReadError",
    "ContentBody",
    "ContentSummary",
    "SpaceDocument",
    "SpaceReader",
    "SpaceSelector",
    "TextOf",
]


class ConfluenceReadError(Exception):
    """Confluence не прочитан: транспорт, статус, форма ответа или его поля."""


class AttachmentGoneError(ConfluenceReadError):
    """Вложение снято между списком и скачиванием."""


class AttachmentSummary(BaseModel):
    """Вложение из списка страницы: всё, кроме файла."""

    model_config = ConfigDict(frozen=True)

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

    @classmethod
    def of(cls, item: ConfluenceAttachmentItem, page: ContentSummary) -> Self:
        where = f"attachment {item.id} of {page.kind} {page.id}"

        return cls(
            id=item.id,
            page_id=page.id,
            space_key=page.space_key,
            title=item.title,
            media_type=item.extensions.media_type,
            file_size=item.extensions.file_size,
            version=item.version.number,
            download_path=item.links.download,
            updated_at=Stamp.parse(item.version.when, f"{where}: version.when"),
            author=Stamp.user(item.version.by.username, item.version.by.display_name),
        )


class SpaceSelector(BaseModel):
    """Какие спейсы источника обходить: маски ключей, вид спейса и брать ли
    архивные. Список ключей без glob-символов берётся как есть, и список
    спейсов с сервера для него не запрашивается."""

    model_config = ConfigDict(frozen=True)

    masks: Sequence[str] = Field(min_length=1)
    type: SpaceType
    archived: bool

    def mask(self) -> SpaceMask:
        return SpaceMask.of_masks(self.masks)


class SpaceDocument(BaseModel):
    """Спейс как его видит индексатор; content_hash от имени и описания."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    space_type: str
    status: str
    description: str
    content_hash: str

    @classmethod
    def of(cls, item: ConfluenceSpaceItem) -> Self:
        name = item.name
        if not name:
            name = item.key

        description = item.description_plain
        digest = TextOf.sha256(f"{name}\n{description}")

        return cls(
            key=item.key,
            name=name,
            space_type=item.type,
            status=item.status,
            description=description,
            content_hash=digest,
        )


class ContentSummary(BaseModel):
    """Страница или блог-запись из списка спейса: всё, кроме тела."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: ContentType
    space_key: str
    title: str
    status: str
    version: int
    parent_id: str
    ancestor_titles: Sequence[str]
    labels: Sequence[str]
    created_at: datetime
    updated_at: datetime
    author: str
    last_editor: str
    attachments: Sequence[AttachmentSummary] = ()
    attachments_truncated: bool = False
    """Раскрытие вложений в списке упёрлось в лимит: полный список берётся отдельно."""

    @classmethod
    def of(cls, content: ConfluenceContent, kind: ContentType) -> Self:
        where = f"{kind} {content.id}"

        parent_id = ""
        if content.ancestors:
            parent_id = content.ancestors[-1].id

        summary = cls(
            id=content.id,
            kind=kind,
            space_key=content.space.key,
            title=content.title,
            status=content.status,
            version=content.version.number,
            parent_id=parent_id,
            ancestor_titles=content.ancestor_titles(),
            labels=content.label_names(),
            created_at=Stamp.parse(
                content.history.created_date, f"{where}: createdDate"
            ),
            updated_at=Stamp.parse(content.version.when, f"{where}: version.when"),
            author=Stamp.user(
                content.history.created_by.username,
                content.history.created_by.display_name,
            ),
            last_editor=Stamp.user(
                content.version.by.username, content.version.by.display_name
            ),
        )

        attachments: list[AttachmentSummary] = []
        for item in content.children.attachment.results:
            attachments.append(AttachmentSummary.of(item, summary))

        return summary.model_copy(
            update={
                "attachments": tuple(attachments),
                "attachments_truncated": content.children.attachment.truncated(),
            }
        )


class CommentDocument(BaseModel):
    """Комментарий страницы: метаданные, хэш оригинала и markdown; тело приходит
    вместе со списком, отдельного запроса нет."""

    model_config = ConfigDict(frozen=True)

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

    @classmethod
    def of(
        cls, content: ConfluenceContent, page: ContentSummary, body_format: str
    ) -> Self:
        where = f"comment {content.id} of {page.kind} {page.id}"

        html = content.body_html(body_format)

        return cls(
            id=content.id,
            page_id=page.id,
            space_key=page.space_key,
            location=content.extensions.location,
            version=content.version.number,
            created_at=Stamp.parse(
                content.history.created_date, f"{where}: createdDate"
            ),
            updated_at=Stamp.parse(content.version.when, f"{where}: version.when"),
            author=Stamp.user(
                content.history.created_by.username,
                content.history.created_by.display_name,
            ),
            content_hash=TextOf.sha256(html),
            markdown=TextOf.markdown(html),
        )


class ContentBody(BaseModel):
    """Тело контента: оригинал уже отброшен, остались его хэш, markdown и ссылки
    на другие страницы."""

    model_config = ConfigDict(frozen=True)

    summary: ContentSummary
    content_hash: str
    markdown: str
    links: Sequence[PageLink]


class Stamp:
    """Даты и пользователи из ответа Confluence в значения surface-строки."""

    @staticmethod
    def parse(raw: str, where: str) -> datetime:
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

    @staticmethod
    def user(username: str, display_name: str) -> str:
        if username:
            return username

        return display_name


class TextOf:
    """Преобразование оригинала в текст индекса и хэш оригинала."""

    HEADING_STYLE: ClassVar[str] = "ATX"
    ENCODING: ClassVar[str] = "utf-8"

    @classmethod
    def sha256(cls, original: str) -> str:
        return hashlib.sha256(original.encode(cls.ENCODING)).hexdigest()

    @staticmethod
    def links(html: str, *, page_id: str, title: str) -> tuple[PageLink, ...]:
        """Ссылки на другие страницы из тела; своя страница и внешние отброшены."""
        soup = ConfluenceHtml.parse_html(html)

        return ConfluenceHtml.collect_targets(soup, page_id=page_id, title=title)

    @classmethod
    def markdown(cls, html: str) -> str:
        answer = PageOps.to_markdown(
            {"html": html, "heading_style": cls.HEADING_STYLE, PageOps.ESCAPE: False}
        )

        return str(answer["markdown"]).strip()


class SpaceReader:
    """Запросы одного обхода спейса поверх пагинатора boba-confluence: auth, ретраи
    и дамп берутся из ConfluenceConnection."""

    SPACE_EXPAND: ClassVar[str] = "description.plain"
    LIST_EXPAND: ClassVar[str] = (
        "version,space,ancestors,metadata.labels,history,"
        "children.attachment.version,children.attachment.extensions"
    )
    COMMENT_EXPAND: ClassVar[str] = (
        "body.{body_format},version,history,extensions.location,container"
    )
    GONE_STATUS: ClassVar[int] = 404
    CHUNK_SIZE: ClassVar[int] = 1 << 20

    def __init__(self, conn: ConfluenceConnection) -> None:
        self._conn = conn
        self._paginator = ConfluencePaginator(conn)
        self._downloads = CancellableHttpTransport(conn.profile, dump=conn.dump)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._paginator.__aexit__(None, None, None)
        await self._downloads.close()

    async def attachments(
        self, summary: ContentSummary
    ) -> AsyncIterator[AttachmentSummary]:
        """Вложения страницы: из раскрытия списка, а при усечении — полным списком."""
        if not summary.attachments_truncated:
            for item in summary.attachments:
                yield item

            return

        url = ConfluenceRest.attachments_path(summary.id)
        try:
            async for item in self._paginator(url, ConfluenceAttachmentItem):
                yield AttachmentSummary.of(item, summary)
        except (TransportError, ConfluencePayloadError) as exc:
            raise ConfluenceReadError(
                f"confluence attachments of {summary.kind} {summary.id}: {exc}"
            ) from exc

    async def comments(self, summary: ContentSummary) -> AsyncIterator[CommentDocument]:
        """Комментарии страницы с телами: у страницы нет признака их смены, список
        читается на каждом обходе."""
        expand = self.COMMENT_EXPAND.format(body_format=self._conn.body_format)
        url = ConfluenceRest.comments_path(summary.id, expand=expand)
        try:
            async for content in self._paginator(url, ConfluenceContent):
                yield CommentDocument.of(content, summary, self._conn.body_format)
        except (TransportError, ConfluencePayloadError) as exc:
            raise ConfluenceReadError(
                f"confluence comments of {summary.kind} {summary.id}: {exc}"
            ) from exc

    async def download(self, attachment: AttachmentSummary, into: Path) -> str:
        """Файл вложения на диск; возвращает sha256 байтов, посчитанный по дороге."""
        where = f"confluence attachment {attachment.id} {attachment.title!r}"
        digest = hashlib.sha256()
        request = HttpRequest(url=attachment.download_path)

        try:
            async with self._downloads.fetch(request) as resp:
                with into.open("wb") as file:
                    async for chunk in resp.stream:
                        digest.update(chunk)
                        file.write(chunk)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            msg = f"{where}: GET {attachment.download_path} expected 2xx, got {status}"
            if status == self.GONE_STATUS:
                raise AttachmentGoneError(msg) from exc

            raise ConfluenceReadError(msg) from exc
        except httpx.HTTPError as exc:
            raise ConfluenceReadError(
                f"{where}: GET {attachment.download_path}: {type(exc).__name__}: {exc}"
            ) from exc

        return digest.hexdigest()

    async def space_keys(self, selector: SpaceSelector) -> list[str]:
        """Ключи спейсов обхода: перечисление как есть или обход списка сервера
        с отбором по маскам, виду и состоянию."""
        mask = selector.mask()
        if not mask.has_wildcard:
            return list(mask.keys())

        url = ConfluenceRest.space_list_path(selector.type)
        keys: list[str] = []
        try:
            async for item in self._paginator(url, ConfluenceSpaceItem):
                if not item.key:
                    continue

                if not selector.archived and item.status == SpaceStatus.ARCHIVED:
                    continue

                if not mask.matches(item):
                    continue

                keys.append(item.key)
        except (TransportError, ConfluencePayloadError) as exc:
            raise ConfluenceReadError(f"confluence space list: {exc}") from exc

        return keys

    async def space(self, key: str) -> SpaceDocument:
        try:
            item = await self._paginator.one(
                ConfluenceRest.space_path(key, expand=self.SPACE_EXPAND),
                ConfluenceSpaceItem,
            )
        except (TransportError, ConfluencePayloadError) as exc:
            raise ConfluenceReadError(f"confluence space {key}: {exc}") from exc

        return SpaceDocument.of(item)

    async def contents(
        self, key: str, kind: ContentType
    ) -> AsyncIterator[ContentSummary]:
        url = ConfluenceRest.space_content_path(
            key, content_type=kind, expand=self.LIST_EXPAND
        )
        try:
            async for content in self._paginator(url, ConfluenceContent):
                yield ContentSummary.of(content, kind)
        except (TransportError, ConfluencePayloadError) as exc:
            raise ConfluenceReadError(
                f"confluence {kind} list of {key}: {exc}"
            ) from exc

    async def body(self, summary: ContentSummary) -> ContentBody:
        url = ConfluenceRest.page_body_path(
            summary.id, body_format=self._conn.body_format
        )
        try:
            content = await self._paginator.one(url, ConfluenceContent)
        except (TransportError, ConfluencePayloadError) as exc:
            raise ConfluenceReadError(
                f"confluence {summary.kind} {summary.id} body: {exc}"
            ) from exc

        html = content.body_html(self._conn.body_format)

        return ContentBody(
            summary=ContentSummary.of(content, summary.kind),
            content_hash=TextOf.sha256(html),
            markdown=TextOf.markdown(html),
            links=TextOf.links(html, page_id=summary.id, title=summary.title),
        )
