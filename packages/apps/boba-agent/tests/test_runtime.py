import asyncio
from uuid import uuid4

import pytest
from psycopg import sql

from boba.agent import models as m
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.migrate import (
    MigrationScripts,
    MigrationTarget,
    PostgresMigrations,
)


@pytest.mark.anyio
@pytest.mark.integration
async def test_latest_migration_installs_all_tables_on_empty_schema(
    postgres: PostgresConfig,
):
    schema_name = "agent_install_" + uuid4().hex
    migration = MigrationScripts(schema_name).application("boba.agent")
    try:
        await PostgresMigrations().apply([MigrationTarget(postgres, migration)])
        async with await AsyncPostgresPool.dedicated(postgres) as connection:
            rows = await (
                await connection.execute(
                    "select tablename from pg_tables where schemaname = %s",
                    (schema_name,),
                )
            ).fetchall()

        assert {row[0] for row in rows} == {
            "users",
            "roles",
            "user_roles",
            "sessions",
            "records",
            "providers",
        }
    finally:
        async with await AsyncPostgresPool.dedicated(postgres) as connection:
            await connection.execute(
                sql.SQL("drop schema if exists {} cascade").format(
                    sql.Identifier(schema_name)
                )
            )


@pytest.mark.anyio
@pytest.mark.integration
async def test_failed_reference_rolls_back_and_retry_is_idempotent(history):
    from boba.agent.runtime import record

    sid = "write-test-" + uuid4().hex
    value = record(
        m.UserMessageRecord, "test", payload={"content": [m.TextPart(text="hello")]}
    )
    try:
        first = await history.append(sid, [value], expected_seq=0)
        assert await history.append(sid, [value], expected_seq=0) == first
        invalid = record(
            m.MessageAcceptedRecord, "test", payload={"source_record_id": "missing"}
        )
        with pytest.raises(m.HistoryWriteError):
            await history.append(sid, [invalid])
        assert await history.head(sid) == 1
        accepted = record(
            m.MessageAcceptedRecord,
            "test",
            payload={"source_record_id": value.record_id},
        )
        assert (await history.append(sid, [accepted]))[0].seq == 2
        await history.append(sid, [record(m.SessionDeletedRecord, "test")])
        assert await history.append(sid, [value]) == first
        with pytest.raises(m.HistoryWriteError) as deleted:
            await history.append(
                sid,
                [
                    record(
                        m.UserMessageRecord,
                        "test",
                        payload={"content": [m.TextPart(text="Too late")]},
                    )
                ],
            )

        assert deleted.value.code == "session_deleted"
    finally:
        async with history.pool.connection() as conn:
            await conn.execute("delete from boba.records where session_id = %s", (sid,))


@pytest.mark.anyio
@pytest.mark.integration
async def test_concurrent_batches_commit_without_duplicate_or_interleaved_seq(history):
    from boba.agent.runtime import record

    sid = "parallel-write-" + uuid4().hex
    batches = tuple(
        tuple(
            record(
                m.UserMessageRecord,
                "test",
                payload={"content": [m.TextPart(text=f"writer {writer}, item {item}")]},
            )
            for item in range(3)
        )
        for writer in range(16)
    )
    try:
        writes = (history.append(sid, batch) for batch in batches)
        committed = await asyncio.gather(*writes)
        stored = (await history.read(sid)).rows
        assert [row.seq for row in stored] == list(range(1, 49))
        assert len({row.record_id for row in stored}) == 48
        for original, saved in zip(batches, committed, strict=True):
            assert tuple(row.record_id for row in saved) == tuple(
                row.record_id for row in original
            )
            assert saved[0].seq is not None
            assert tuple(row.seq for row in saved) == tuple(
                range(saved[0].seq, saved[0].seq + 3)
            )
            assert await history.append(sid, original) == saved

        assert await history.head(sid) == 48

    finally:
        async with history.pool.connection() as connection:
            await connection.execute(
                "delete from boba.records where session_id = %s", (sid,)
            )
