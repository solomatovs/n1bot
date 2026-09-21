"""Реестр таблиц поисковых индексов: какие таблицы какого вида есть в схеме.

Таблицу создаёт её владелец, а поиск не знает имени: он читает реестр нужного вида и
опрашивает каждую таблицу своим запросом. Строки вписывают владельцы файлами схемы,
накат проверяет, что таблица существует и несёт колонки вида.

В реестре только вид, имя и владелец. Специфика вида — модель эмбеддинга и её
размерность у вектора, языковая конфигурация у полнотекста — живёт у владельца:
ядру она не нужна, а копия чужого конфига здесь только разошлась бы с оригиналом.

Ошибки:
IndexTableError — зарегистрированная таблица отсутствует или не несёт колонок,
    которых требует её вид.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, LiteralString

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from boba.ix_core.schema_name import SchemaName

__all__ = ["IndexKind", "IndexTable", "IndexTableError", "IndexTables"]


class IndexTableError(Exception):
    """Таблица индекса не отвечает контракту своего вида."""


class IndexKind(StrEnum):
    """Вид поискового индекса; значения index_kind_e."""

    TRGM = "trgm"
    FTS = "fts"
    VECTOR = "vector"


class IndexTable(BaseModel):
    """Одна строка {schema}.index_table: таблица индекса и её вид."""

    model_config = ConfigDict(frozen=True)

    kind: IndexKind
    name: str
    owner: str

    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.name)


class IndexColumns:
    """Колонки, которые обязана нести таблица каждого вида."""

    COMMON: ClassVar[tuple[str, ...]] = ("node_id", "surface", "aspect", "content")
    BY_KIND: ClassVar[Mapping[IndexKind, tuple[str, ...]]] = {
        IndexKind.TRGM: COMMON,
        IndexKind.FTS: (*COMMON, "tsv"),
        IndexKind.VECTOR: (*COMMON, "chunk_no", "content_hash", "emb"),
    }

    @classmethod
    def of(cls, kind: IndexKind) -> tuple[str, ...]:
        return cls.BY_KIND[kind]


class IndexTables:
    """Чтение реестра и проверка его строк."""

    ALL: ClassVar[str] = """
        select
            t.kind::varchar,
            t.name,
            t.owner
        from
            {schema}.index_table t
        order by
            t.kind,
            t.name
    """
    COLUMNS: ClassVar[LiteralString] = """
        select
            c.column_name
        from
            information_schema.columns c
        where
            c.table_schema = %(schema)s
            and c.table_name = %(name)s
    """
    PROBE: ClassVar[LiteralString] = "select 1 from {schema}.{name} limit 0"

    @classmethod
    async def all(
        cls, conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> list[IndexTable]:
        cur = await conn.execute(SchemaName.render(cls.ALL, db_schema))
        rows = await cur.fetchall()

        return list(cls._rows(rows))

    @classmethod
    async def of_kind(
        cls,
        conn: psycopg.AsyncConnection[Any],
        db_schema: str,
        kind: IndexKind,
    ) -> list[IndexTable]:
        tables: list[IndexTable] = []
        for table in await cls.all(conn, db_schema):
            if table.kind is kind:
                tables.append(table)

        return tables

    @classmethod
    async def check(
        cls,
        conn: psycopg.AsyncConnection[Any],
        db_schema: str,
        tables: Sequence[IndexTable],
    ) -> None:
        for table in tables:
            await cls._check_one(conn, db_schema, table)

    @classmethod
    async def _check_one(
        cls, conn: psycopg.AsyncConnection[Any], db_schema: str, table: IndexTable
    ) -> None:
        where = f"index table {table.name} of kind {table.kind} (owner {table.owner})"

        probe = sql.SQL(cls.PROBE).format(
            schema=sql.Identifier(db_schema), name=table.ident()
        )
        try:
            await conn.execute(probe)
        except psycopg.Error as exc:
            raise IndexTableError(f"{where}: table is not readable: {exc}") from exc

        cur = await conn.execute(cls.COLUMNS, {"schema": db_schema, "name": table.name})
        present: set[str] = set()
        for row in await cur.fetchall():
            present.add(str(row[0]))

        missing: list[str] = []
        for column in IndexColumns.of(table.kind):
            if column not in present:
                missing.append(column)

        if not missing:
            return

        raise IndexTableError(
            f"{where}: expected columns {list(IndexColumns.of(table.kind))}, "
            f"missing {missing}"
        )

    @staticmethod
    def _rows(rows: Iterable[Sequence[Any]]) -> Iterator[IndexTable]:
        for kind, name, owner in rows:
            yield IndexTable(
                kind=IndexKind(str(kind)), name=str(name), owner=str(owner)
            )
