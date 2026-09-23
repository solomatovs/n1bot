"""PostgresTable на живой базе стенда: DDL под замком, запросы, граница ошибок
слоя, проверка раскладки и перенос таблиц между схемами.

pytest -m integration.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import ClassVar

import pytest
from psycopg import sql

from boba.db.postgres import (
    AdvisoryLock,
    AsyncPostgresPool,
    PgQuery,
    PgQueryBuilder,
    PostgresError,
    PostgresSchema,
    PostgresTable,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "table_test"
OTHER = "table_test_other"


class NotesError(Exception):
    """Ошибка слоя тестового хранилища."""


class NotesTable(PostgresTable):
    """Тестовое хранилище: одна таблица заметок и своя ошибка слоя."""

    LABEL: ClassVar[str] = "notes"

    def _failure(self, action: str, exc: Exception) -> Exception:
        return NotesError(self._detail(action, exc))

    def ddl(self) -> tuple[PgQuery, ...]:
        return (
            self._query()
            .add(
                """
                create table if not exists {schema}.notes (
                    id   serial primary key,
                    text text not null
                )
                """
            )
            .build(),
        )

    async def setup(self) -> None:
        await self._apply_ddl(self.ddl())

    async def add(self, text: str) -> int:
        query = (
            self._query()
            .add(
                "insert into {schema}.notes (text) values (%(text)s) returning id",
                text=text,
            )
            .build()
        )
        row = self._returning(await self._row(query, "add"), f"insert of {text!r}")

        return int(row["id"])

    async def texts(self) -> list[str]:
        query = self._query().add("select text from {schema}.notes order by id").build()
        rows = await self._rows(query, "list")

        texts: list[str] = []
        for row in rows:
            texts.append(str(row["text"]))

        return texts

    async def clear(self) -> int:
        query = self._query().add("delete from {schema}.notes").build()

        return await self._execute(query, "clear")

    async def broken(self) -> None:
        query = self._query().add("select 1 from {schema}.no_such_table").build()
        await self._row(query, "reading a missing table")

    async def check(self, columns: list[str]) -> None:
        await self._check_layouts({"notes": columns})


async def _drop(pool: AsyncPostgresPool, *schemas: str) -> None:
    async with pool.connection() as conn:
        for schema in schemas:
            drop = (
                PgQueryBuilder(schema=sql.Identifier(schema))
                .add("drop schema if exists {schema} cascade")
                .build()
            )
            await conn.execute(drop.text, drop.params)


@pytest.fixture
async def notes(pool: AsyncPostgresPool) -> AsyncIterator[NotesTable]:
    await _drop(pool, SCHEMA, OTHER)
    table = NotesTable(None, SCHEMA, pool)
    await table.setup()
    try:
        yield table
    finally:
        await _drop(pool, SCHEMA, OTHER)


async def test_ddl_is_idempotent_and_queries_round_trip(notes: NotesTable) -> None:
    await notes.setup()

    first = await notes.add("one")
    second = await notes.add("two")
    assert second == first + 1
    assert await notes.texts() == ["one", "two"]
    assert await notes.clear() == 2
    assert await notes.texts() == []


async def test_database_failure_becomes_the_layer_error(notes: NotesTable) -> None:
    with pytest.raises(NotesError) as refused:
        await notes.broken()

    text = str(refused.value)
    assert text.startswith("notes: reading a missing table in schema table_test")
    assert "no_such_table" in text


async def test_layout_check_names_the_drift(notes: NotesTable) -> None:
    await notes.check(["id", "text"])

    with pytest.raises(NotesError) as refused:
        await notes.check(["id", "body"])

    text = str(refused.value)
    assert f"{SCHEMA}.notes" in text
    assert "missing columns ['body']" in text
    assert "unexpected columns ['text']" in text
    assert "drop the schema" in text


async def test_table_moves_between_schemas(
    pool: AsyncPostgresPool, notes: NotesTable
) -> None:
    await notes.add("kept")
    source = PostgresSchema(SCHEMA)
    target = PostgresSchema(OTHER)

    async with pool.connection() as conn:
        await target.ensure(conn)
        await source.move_table(conn, "notes", target)
        assert not await source.has_table(conn, "notes")
        assert await target.has_table(conn, "notes")
        assert await target.columns_of(conn, "notes") == {"id", "text"}

    # пустой дубль на старом месте сносится, строки остаются в новой схеме
    await notes.setup()
    async with pool.connection() as conn:
        await source.move_table(conn, "notes", target)
        assert not await source.has_table(conn, "notes")

    moved = NotesTable(None, OTHER, pool)
    assert await moved.texts() == ["kept"]

    # строки с обеих сторон переносить нельзя
    await notes.setup()
    await notes.add("clash")
    async with pool.connection() as conn:
        with pytest.raises(PostgresError) as refused:
            await source.move_table(conn, "notes", target)

    assert "merge them by hand" in str(refused.value)


async def test_advisory_lock_serialises_transactions(pool: AsyncPostgresPool) -> None:
    lock = AdvisoryLock("table_test.lock")
    released = asyncio.Event()

    async def holder() -> None:
        async with pool.connection() as conn, conn.transaction():
            await lock.acquire(conn)
            await released.wait()

    holding = asyncio.create_task(holder())
    await asyncio.sleep(0.2)

    async def waiter() -> None:
        async with pool.connection() as conn, conn.transaction():
            await lock.acquire(conn)

    waiting = asyncio.create_task(waiter())
    done, _pending = await asyncio.wait({waiting}, timeout=0.5)
    assert not done, "the second transaction must wait for the first"

    released.set()
    await holding
    await asyncio.wait_for(waiting, timeout=5)
