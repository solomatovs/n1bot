"""Карточка node для чтения: адрес и ссылка, путь по tree, тексты аспектов.

Поиск отдаёт node_id и сниппет, а полный текст объекта лежит строками полнотекстовых
таблиц из реестра: markdown страницы, текст вложения, описание таблицы. NodeReader
собирает по node_id одну карточку: поверхность, адрес, ссылку по формуле владельца,
цепочку родителей от корня и тексты запрошенных аспектов с их классами.

Подпись родителя это самый короткий текст его аспекта класса ident (имя, а не путь);
у объекта без такого аспекта подписью служит ссылка.

Ошибки:
NodeReadError — node с таким id нет или база отклонила запрос.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, LiteralString

import psycopg
from pydantic import BaseModel, ConfigDict

from boba.ix_core.aspects import AspectClass
from boba.ix_core.indexes import IndexKind, IndexTable
from boba.ix_core.schema_name import SchemaName
from boba.ix_core.search import SearchRegistry

__all__ = ["NodeCard", "NodeReadError", "NodeReader", "NodeStep", "NodeText"]


class NodeReadError(Exception):
    """Карточку node не собрать."""


class NodeStep(BaseModel):
    """Один родитель в пути от корня tree: объект и его подпись."""

    model_config = ConfigDict(frozen=True)

    node_id: int
    surface: str
    label: str


class NodeText(BaseModel):
    """Текст одного аспекта объекта."""

    model_config = ConfigDict(frozen=True)

    aspect: str
    aspect_class: AspectClass
    content: str


class NodeCard(BaseModel):
    """Всё, что известно об объекте для чтения."""

    model_config = ConfigDict(frozen=True)

    node_id: int
    surface: str
    address: Mapping[str, Any]
    url: str
    parents: tuple[NodeStep, ...]
    texts: tuple[NodeText, ...]


class NodeRow(BaseModel):
    """Строка {schema}.node до сборки карточки."""

    model_config = ConfigDict(frozen=True)

    node_id: int
    surface: str
    address: Mapping[str, Any]


class NodeReader:
    """Сборка карточки node на переданном соединении по реестрам вызывающего."""

    NODE: ClassVar[LiteralString] = """
        select
            n.id,
            n.surface::varchar,
            n.address
        from
            {schema}.node n
        where
            n.id = %(node_id)s
    """
    PARENTS: ClassVar[LiteralString] = """
        with recursive up as (
            select
                t.parent_id as id,
                1 as depth
            from
                {schema}.tree t
            where
                t.node_id = %(node_id)s
            union all
            select
                t.parent_id,
                up.depth + 1
            from
                {schema}.tree t
                join up on t.node_id = up.id
            where
                up.id is not null
        )
        select
            n.id,
            n.surface::varchar,
            n.address
        from
            up
            join {schema}.node n on n.id = up.id
        order by
            up.depth desc
    """
    LABELS: ClassVar[LiteralString] = """
        select
            f.node_id,
            f.content
        from
            {schema}.{index} f
            join {schema}.aspect a on a.aspect = f.aspect
        where 1=1
            and f.node_id = any(%(ids)s)
            and a.class = 'ident'
        order by
            f.node_id,
            length(f.content)
    """
    TEXTS: ClassVar[LiteralString] = """
        select
            f.aspect::varchar,
            a.class::varchar,
            f.content
        from
            {schema}.{index} f
            join {schema}.aspect a on a.aspect = f.aspect
        where 1=1
            and f.node_id = %(node_id)s
            and f.aspect = any(%(aspects)s::{schema}.aspect_e[])
        order by
            a.class,
            f.aspect
    """

    def __init__(self, db_schema: str, registry: SearchRegistry) -> None:
        self._schema = db_schema
        self._registry = registry

    async def read(
        self,
        conn: psycopg.AsyncConnection[Any],
        node_id: int,
        aspects: Sequence[str],
    ) -> NodeCard:
        """Карточка node; пустой список аспектов значит все из словаря."""
        chosen = self._aspects_of(aspects)

        try:
            node = await self._node(conn, node_id)
            parents = await self._parents(conn, node_id)
            texts = await self._texts(conn, node_id, chosen)
        except psycopg.Error as exc:
            raise NodeReadError(
                f"reading node {node_id} from {self._schema}: {exc}"
            ) from exc

        return NodeCard(
            node_id=node.node_id,
            surface=node.surface,
            address=node.address,
            url=self._registry.urls.of(node.surface, node.address),
            parents=parents,
            texts=texts,
        )

    def _aspects_of(self, chosen: Sequence[str]) -> list[str]:
        catalog = self._registry.aspects
        unknown = catalog.unknown(chosen)
        if unknown:
            raise NodeReadError(
                f"node: aspects {list(unknown)} are not declared in "
                f"{self._schema}.aspect; known are {list(catalog.names())}"
            )

        if chosen:
            return list(chosen)

        return list(catalog.names())

    def _fts_tables(self) -> tuple[IndexTable, ...]:
        tables = self._registry.tables_of(IndexKind.FTS)
        if not tables:
            raise NodeReadError(
                f"node: the registry {self._schema}.index_table has no fts table "
                "to read texts from"
            )

        return tables

    async def _node(
        self, conn: psycopg.AsyncConnection[Any], node_id: int
    ) -> NodeRow:
        cur = await conn.execute(
            SchemaName.render(self.NODE, self._schema), {"node_id": node_id}
        )
        row = await cur.fetchone()
        if row is None:
            raise NodeReadError(f"node {node_id} not found in {self._schema}.node")

        found_id, surface, address = row

        return NodeRow(node_id=int(found_id), surface=str(surface), address=address)

    async def _parents(
        self, conn: psycopg.AsyncConnection[Any], node_id: int
    ) -> tuple[NodeStep, ...]:
        cur = await conn.execute(
            SchemaName.render(self.PARENTS, self._schema), {"node_id": node_id}
        )
        rows: list[NodeRow] = []
        for found_id, surface, address in await cur.fetchall():
            rows.append(
                NodeRow(node_id=int(found_id), surface=str(surface), address=address)
            )

        if not rows:
            return ()

        ids: list[int] = []
        for row in rows:
            ids.append(row.node_id)

        labels = await self._labels(conn, ids)

        steps: list[NodeStep] = []
        for row in rows:
            label = labels.get(row.node_id)
            if label is None:
                label = self._fallback_label(row)

            steps.append(
                NodeStep(node_id=row.node_id, surface=row.surface, label=label)
            )

        return tuple(steps)

    async def _labels(
        self, conn: psycopg.AsyncConnection[Any], ids: Sequence[int]
    ) -> dict[int, str]:
        """Первая (самая короткая) ident-строка каждого node по всем fts-таблицам."""
        labels: dict[int, str] = {}
        for table in self._fts_tables():
            query = SchemaName.render(self.LABELS, self._schema, index=table.ident())
            cur = await conn.execute(query, {"ids": list(ids)})
            for found_id, content in await cur.fetchall():
                if int(found_id) in labels:
                    continue

                labels[int(found_id)] = str(content)

        return labels

    def _fallback_label(self, row: NodeRow) -> str:
        url = self._registry.urls.of(row.surface, row.address)
        if url:
            return url

        return json.dumps(dict(row.address), ensure_ascii=False)

    async def _texts(
        self,
        conn: psycopg.AsyncConnection[Any],
        node_id: int,
        aspects: Sequence[str],
    ) -> tuple[NodeText, ...]:
        texts: list[NodeText] = []
        for table in self._fts_tables():
            query = SchemaName.render(self.TEXTS, self._schema, index=table.ident())
            cur = await conn.execute(query, {"node_id": node_id, "aspects": list(aspects)})
            for aspect, aspect_class, content in await cur.fetchall():
                texts.append(
                    NodeText(
                        aspect=str(aspect),
                        aspect_class=AspectClass(str(aspect_class)),
                        content=str(content),
                    )
                )

        return tuple(texts)
