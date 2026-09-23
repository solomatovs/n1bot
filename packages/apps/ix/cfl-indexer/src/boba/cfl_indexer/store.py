"""Запись в ix одним соединением: node и tree, surface-строки cfl_*, текст в общий
полнотекст {schema}.ix_fts (только body и ocr), ссылки в рёбра, чистка спейса.
Запросы — файлы run/, прочитанные один раз.

Ошибки:
IxWriteError — база отдала не то, что ждали; psycopg.Error уходит наружу.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from boba.cfl_indexer.confluence import Attachment, Comment, Content, Space
from boba.confluence.models import PageLink
from boba.confluence.rest import ContentType
from boba.db.postgres.query import PgQueryBuilder

__all__ = [
    "Aspect",
    "IxStore",
    "IxWriteError",
    "RunFile",
    "State",
    "Surface",
]

PUSHED_ASPECTS = ["body", "ocr"]


class IxWriteError(Exception):
    """База ответила не тем, что ждал индексатор."""


class Surface(StrEnum):
    SPACE = "cfl_space"
    PAGE = "cfl_page"
    BLOGPOST = "cfl_blogpost"
    ATTACHMENT = "cfl_attachment"
    COMMENT = "cfl_comment"
    PAGE_LINK = "cfl_page_link"


class Aspect(StrEnum):
    BODY = "body"
    OCR = "ocr"


class RunFile(StrEnum):
    NODE = "10_node.sql"
    TREE = "11_tree.sql"
    SPACE_STATE = "20_space_state.sql"
    SPACE = "21_space.sql"
    PAGE_STATE = "22_page_state.sql"
    PAGE = "23_page.sql"
    BLOGPOST_STATE = "24_blogpost_state.sql"
    BLOGPOST = "25_blogpost.sql"
    ATTACHMENT_STATE = "26_attachment_state.sql"
    ATTACHMENT = "27_attachment.sql"
    COMMENT_STATE = "28_comment_state.sql"
    COMMENT = "29_comment.sql"
    SEEN_TABLE = "30_seen_table.sql"
    SEEN_MARK = "31_seen_mark.sql"
    LINKS_TABLE = "32_links_table.sql"
    LINKS_MARK = "33_links_mark.sql"
    INDEX_CLEAR = "50_index_clear.sql"
    INDEX_PUSH = "51_index_push.sql"
    LINKS_CLEAR = "80_links_clear.sql"
    LINKS_APPLY = "81_links_apply.sql"
    SWEEP = "90_sweep.sql"


class State(NamedTuple):
    """Что surface-строка помнит о node с прошлого прогона."""

    version: int
    content_hash: str
    indexer_hash: str


class IxStore:
    """Запись одного обхода спейса через одно соединение."""

    def __init__(
        self, package_dir: Path, db_schema: str, conn: psycopg.AsyncConnection[Any]
    ) -> None:
        self._conn = conn
        self._schema = db_schema
        self._texts: dict[RunFile, str] = {}
        for name in RunFile:
            self._texts[name] = (package_dir / name).read_text(encoding="utf-8")

    def surface_of(self, kind: ContentType) -> Surface:
        if kind is ContentType.BLOGPOST:
            return Surface.BLOGPOST

        return Surface.PAGE

    def state_file_of(self, kind: ContentType) -> RunFile:
        if kind is ContentType.BLOGPOST:
            return RunFile.BLOGPOST_STATE

        return RunFile.PAGE_STATE

    def row_file_of(self, kind: ContentType) -> RunFile:
        if kind is ContentType.BLOGPOST:
            return RunFile.BLOGPOST

        return RunFile.PAGE

    async def _execute(self, file: RunFile, **params: Any) -> psycopg.AsyncCursor[Any]:
        query = (
            PgQueryBuilder(schema=sql.Identifier(self._schema))
            .add(self._texts[file], **params)
            .build()
        )

        return await self._conn.execute(query.text, query.params)

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None, None]:
        async with self._conn.transaction():
            yield

    async def create_tables(self) -> None:
        await self._execute(RunFile.SEEN_TABLE)
        await self._execute(RunFile.LINKS_TABLE)

    async def upsert_node(self, surface: Surface, address: Mapping[str, object]) -> int:
        cur = await self._execute(
            RunFile.NODE, surface=str(surface), address=Jsonb(dict(address))
        )
        row = await cur.fetchone()
        if row is None:
            raise IxWriteError(f"node {surface} {dict(address)}: expected an id")

        return int(row[0])

    async def attach_to_parent(self, node_id: int, parent_id: int | None) -> None:
        await self._execute(RunFile.TREE, node_id=node_id, parent_id=parent_id)

    async def mark_seen(self, node_id: int) -> None:
        await self._execute(RunFile.SEEN_MARK, node_id=node_id)

    async def read_state(self, file: RunFile, node_id: int) -> State | None:
        cur = await self._execute(file, node_id=node_id)
        row = await cur.fetchone()
        if row is None:
            return None

        return State(int(row[0]), str(row[1]), str(row[2]))

    async def write_space(self, node_id: int, space: Space, indexer_hash: str) -> None:
        await self._execute(
            RunFile.SPACE,
            node_id=node_id,
            space_key=space.key,
            name=space.name,
            space_type=space.space_type,
            status=space.status,
            description=space.description,
            content_hash=space.content_hash,
            indexer_hash=indexer_hash,
        )

    async def write_content(
        self, node_id: int, content: Content, indexer_hash: str
    ) -> None:
        params: dict[str, object] = {
            "node_id": node_id,
            "space_key": content.space_key,
            "content_id": content.id,
            "title": content.title,
            "status": content.status,
            "version": content.version,
            "created_at": content.created_at,
            "updated_at": content.updated_at,
            "author": content.author,
            "last_editor": content.last_editor,
            "labels": list(content.labels),
            "content_hash": content.content_hash,
            "indexer_hash": indexer_hash,
        }
        if content.kind is ContentType.PAGE:
            params["ancestor_titles"] = list(content.ancestor_titles)

        await self._execute(self.row_file_of(content.kind), **params)

    async def write_attachment(
        self,
        node_id: int,
        attachment: Attachment,
        content_hash: str,
        indexer_hash: str,
    ) -> None:
        await self._execute(
            RunFile.ATTACHMENT,
            node_id=node_id,
            space_key=attachment.space_key,
            page_id=attachment.page_id,
            attachment_id=attachment.id,
            title=attachment.title,
            media_type=attachment.media_type,
            file_size=attachment.file_size,
            version=attachment.version,
            created_at=attachment.updated_at,
            updated_at=attachment.updated_at,
            author=attachment.author,
            content_hash=content_hash,
            indexer_hash=indexer_hash,
        )

    async def write_comment(
        self, node_id: int, comment: Comment, indexer_hash: str
    ) -> None:
        await self._execute(
            RunFile.COMMENT,
            node_id=node_id,
            space_key=comment.space_key,
            page_id=comment.page_id,
            comment_id=comment.id,
            location=comment.location,
            version=comment.version,
            created_at=comment.created_at,
            updated_at=comment.updated_at,
            author=comment.author,
            content_hash=comment.content_hash,
            indexer_hash=indexer_hash,
        )

    async def clear_texts(self, node_id: int) -> None:
        await self._execute(
            RunFile.INDEX_CLEAR, node_id=node_id, aspects=PUSHED_ASPECTS
        )

    async def push_text(
        self, node_id: int, surface: Surface, aspect: Aspect, text: str
    ) -> None:
        if not text:
            return

        await self._execute(
            RunFile.INDEX_PUSH,
            node_id=node_id,
            surface=str(surface),
            aspect=str(aspect),
            content=text,
        )

    async def queue_links(self, node_id: int, links: Sequence[PageLink]) -> None:
        """Ссылки страницы в temp-таблицу; рёбрами они станут после обхода."""
        for link in links:
            await self._execute(
                RunFile.LINKS_MARK,
                src_node=node_id,
                target_id=link.target.page_id,
                target_title=link.target.title,
                kind=str(link.kind),
            )

    async def apply_links(self, space_key: str, base: Mapping[str, object]) -> int:
        async with self._conn.transaction():
            await self._execute(RunFile.LINKS_CLEAR)
            cur = await self._execute(
                RunFile.LINKS_APPLY, space_key=space_key, base=Jsonb(dict(base))
            )
            row = await cur.fetchone()

        if row is None:
            raise IxWriteError(f"links of space {space_key}: expected a summary row")

        return int(row[0])

    async def sweep_space(self, space_key: str, base: Mapping[str, object]) -> int:
        cur = await self._execute(
            RunFile.SWEEP, space_key=space_key, base=Jsonb(dict(base))
        )
        row = await cur.fetchone()
        if row is None:
            raise IxWriteError(f"sweep of space {space_key}: expected a summary row")

        return int(row[0])
