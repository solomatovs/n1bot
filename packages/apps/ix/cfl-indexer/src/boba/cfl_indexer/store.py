"""Запись индексатора в ix: node и tree, surface-строки cfl_*, текст в общий полнотекст
и чистка спейса. Все запросы — файлы run/ с именованными параметрами.

Node пишется одной транзакцией: surface-строка и текст, который SQL добыть не может
(markdown страницы, текст вложения, OCR), в {schema}.ix_fts. Всё остальное делают общие
индексаторы: ix-fts выводит из объявлений title, path, card и выравнивает веса, ix-trgm
и ix-vector берут свои классы аспектов. Поэтому объявление, добавленное описателем или
другим потребителем, попадает в индексы само, без обхода Confluence.

Ошибки:
IxWriteError — база отдала не то, что ждали: нет id node, нет сводки шага.
psycopg.Error уходит вызывающему, его упаковывает граница прогона.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from boba.confluence.models import PageLink
from boba.ix_core.schema_name import SchemaName

__all__ = [
    "Aspect",
    "IxWriteError",
    "IxWriter",
    "NodeState",
    "PackageSql",
    "PushedAspects",
    "PushedText",
    "RowWrite",
    "RunFile",
    "Surface",
]


class IxWriteError(Exception):
    """База ответила не тем, что ждал индексатор."""


class Surface(StrEnum):
    """Поверхности, которыми владеет индексатор; значения surface_e."""

    SPACE = "cfl_space"
    PAGE = "cfl_page"
    BLOGPOST = "cfl_blogpost"
    ATTACHMENT = "cfl_attachment"
    COMMENT = "cfl_comment"
    PAGE_LINK = "cfl_page_link"


class Aspect(StrEnum):
    """Аспекты индексатора; значения aspect_e."""

    TITLE = "title"
    PATH = "path"
    WORDS = "words"
    LABELS = "labels"
    CARD = "card"
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
    PUSHED_TEXTS = "40_pushed_texts.sql"
    INDEX_CLEAR = "50_index_clear.sql"
    INDEX_PUSH = "51_index_push.sql"
    LINKS_CLEAR = "80_links_clear.sql"
    LINKS_APPLY = "81_links_apply.sql"
    SWEEP = "90_sweep.sql"


class PushedAspects:
    """Аспекты, текст которых кладёт сам индексатор; остальные выводятся из
    объявлений общими индексаторами."""

    @staticmethod
    def names() -> list[str]:
        return [str(Aspect.BODY), str(Aspect.OCR)]


class NodeState(BaseModel):
    """Что surface-строка помнит о node с прошлого прогона."""

    model_config = ConfigDict(frozen=True)

    version: int
    content_hash: str
    indexer_hash: str

    def unchanged(self, version: int, indexer_hash: str) -> bool:
        if self.version != version:
            return False

        return self.indexer_hash == indexer_hash


class PushedText(BaseModel):
    """Текст аспекта, который кладётся в полнотекст напрямую."""

    model_config = ConfigDict(frozen=True)

    aspect: Aspect
    content: str


class RowWrite(BaseModel):
    """Surface-строка node: файл upsert и его параметры."""

    model_config = ConfigDict(frozen=True)

    file: RunFile
    params: Mapping[str, object]


class PackageSql:
    """Файлы run/ пакета под схему графа."""

    def __init__(self, package_dir: Path, db_schema: str) -> None:
        self._dir = package_dir
        self._db_schema = db_schema

    def load(self, name: RunFile) -> sql.Composed:
        text = (self._dir / name).read_text(encoding="utf-8")

        return SchemaName.render(text, self._db_schema)


class IxWriter:
    """Запись одного node и чистка спейса; соединение приходит в каждый вызов."""

    def __init__(self, sql_files: PackageSql) -> None:
        self._sql = sql_files

    async def node(
        self,
        conn: psycopg.AsyncConnection[Any],
        surface: Surface,
        address: Mapping[str, object],
    ) -> int:
        cur = await conn.execute(
            self._sql.load(RunFile.NODE),
            {"surface": str(surface), "address": Jsonb(dict(address))},
        )
        row = await cur.fetchone()
        if row is None:
            raise IxWriteError(
                f"node {surface} {dict(address)}: expected an id, got none"
            )

        return int(row[0])

    async def tree(
        self, conn: psycopg.AsyncConnection[Any], node_id: int, parent_id: int | None
    ) -> None:
        await conn.execute(
            self._sql.load(RunFile.TREE), {"node_id": node_id, "parent_id": parent_id}
        )

    async def seen_table(self, conn: psycopg.AsyncConnection[Any]) -> None:
        await conn.execute(self._sql.load(RunFile.SEEN_TABLE))
        await conn.execute(self._sql.load(RunFile.LINKS_TABLE))

    async def links(
        self,
        conn: psycopg.AsyncConnection[Any],
        node_id: int,
        links: Sequence[PageLink],
    ) -> None:
        """Ссылки страницы в temp-таблицу; рёбрами они станут после обхода."""
        for link in links:
            await conn.execute(
                self._sql.load(RunFile.LINKS_MARK),
                {
                    "src_node": node_id,
                    "target_id": link.target.page_id,
                    "target_title": link.target.title,
                    "kind": str(link.kind),
                },
            )

    async def apply_links(
        self,
        conn: psycopg.AsyncConnection[Any],
        space_key: str,
        base: Mapping[str, object],
    ) -> int:
        """Ссылки обхода в рёбра cfl_page_link; возвращает число новых рёбер."""
        async with conn.transaction():
            await conn.execute(self._sql.load(RunFile.LINKS_CLEAR))
            cur = await conn.execute(
                self._sql.load(RunFile.LINKS_APPLY),
                {"space_key": space_key, "base": Jsonb(dict(base))},
            )
            row = await cur.fetchone()

        if row is None:
            raise IxWriteError(
                f"links of space {space_key}: expected a summary row, got none"
            )

        return int(row[0])

    async def seen(self, conn: psycopg.AsyncConnection[Any], node_id: int) -> None:
        await conn.execute(self._sql.load(RunFile.SEEN_MARK), {"node_id": node_id})

    async def state(
        self, conn: psycopg.AsyncConnection[Any], name: RunFile, node_id: int
    ) -> NodeState | None:
        cur = await conn.execute(self._sql.load(name), {"node_id": node_id})
        row = await cur.fetchone()
        if row is None:
            return None

        return NodeState(
            version=int(row[0]), content_hash=str(row[1]), indexer_hash=str(row[2])
        )

    async def pushed_texts(
        self, conn: psycopg.AsyncConnection[Any], node_id: int
    ) -> list[PushedText]:
        """Тексты body и ocr, которые node уже несёт в полнотексте."""
        cur = await conn.execute(
            self._sql.load(RunFile.PUSHED_TEXTS), {"node_id": node_id}
        )
        texts: list[PushedText] = []
        for aspect, content in await cur.fetchall():
            texts.append(PushedText(aspect=Aspect(str(aspect)), content=str(content)))

        return texts

    async def write_node(
        self,
        conn: psycopg.AsyncConnection[Any],
        node_id: int,
        surface: Surface,
        row: RowWrite,
        pushed: Sequence[PushedText],
    ) -> None:
        """Surface-строка и текст node одной транзакцией: сорвался шаг — node
        остаётся прежним, и следующий прогон делает его заново."""
        async with conn.transaction():
            await conn.execute(self._sql.load(row.file), dict(row.params))
            await conn.execute(
                self._sql.load(RunFile.INDEX_CLEAR),
                {"node_id": node_id, "aspects": PushedAspects.names()},
            )

            for text in pushed:
                if not text.content:
                    continue

                await conn.execute(
                    self._sql.load(RunFile.INDEX_PUSH),
                    {
                        "node_id": node_id,
                        "surface": str(surface),
                        "aspect": str(text.aspect),
                        "content": text.content,
                    },
                )

    async def sweep(
        self,
        conn: psycopg.AsyncConnection[Any],
        space_key: str,
        base: Mapping[str, object],
    ) -> int:
        cur = await conn.execute(
            self._sql.load(RunFile.SWEEP),
            {"space_key": space_key, "base": Jsonb(dict(base))},
        )
        row = await cur.fetchone()
        if row is None:
            raise IxWriteError(
                f"sweep of space {space_key}: expected a summary row, got none"
            )

        return int(row[0])
