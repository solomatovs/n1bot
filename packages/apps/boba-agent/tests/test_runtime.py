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
            "tasks",
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
    from boba.agent.records import RecordFactory

    sid = "write-test-" + uuid4().hex
    value = RecordFactory("test").make(
        m.UserMessageRecord, payload={"content": [m.TextPart(text="hello")]}
    )
    try:
        first = await history.append(sid, [value], expected_seq=0)
        assert await history.append(sid, [value], expected_seq=0) == first
        invalid = RecordFactory("test").make(
            m.ToolResultRecord,
            key="missing-call",
            payload={"call_record_id": "missing", "status": "completed"},
        )
        with pytest.raises(m.HistoryWriteError):
            await history.append(sid, [invalid])
        assert await history.head(sid) == 1
        await history.append(
            sid,
            [
                RecordFactory("test").make(
                    m.SessionDeletedRecord,
                )
            ],
        )
        assert await history.append(sid, [value]) == first
        with pytest.raises(m.HistoryWriteError) as deleted:
            await history.append(
                sid,
                [
                    RecordFactory("test").make(
                        m.UserMessageRecord,
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
    from boba.agent.records import RecordFactory

    sid = "parallel-write-" + uuid4().hex
    batches = tuple(
        tuple(
            RecordFactory("test").make(
                m.UserMessageRecord,
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


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.parametrize("last_override", ["value", "absent", "remove"])
async def test_pending_batch_uses_only_last_operation_settings(
    history, agent_timeout, last_override, task_runtime
):
    from agent_stand import Llm

    class BlockingTool:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def call(self, agent, reply, call):
            self.started.set()
            await self.release.wait()
            return (m.TextPart(text="Completed"),)

    sid = "batch-settings-" + uuid4().hex
    llm = Llm()
    tool = BlockingTool()
    agent = task_runtime.create(sid, "test", llm, tool)
    try:
        await history.create_session(
            sid,
            "test",
            "/workspace",
            [
                agent.make(
                    m.ModelRecord,
                    payload={"provider": "test", "value": {"model": "test"}},
                ),
                agent.make(m.SamplingRecord, payload={"value": {"temperature": 0.8}}),
                agent.make(
                    m.InferenceRecord, payload={"value": {"max_completion_tokens": 400}}
                ),
            ],
        )
        await agent.submit([m.TextPart(text="Start tool")])
        async with asyncio.timeout(agent_timeout):
            await tool.started.wait()

        async def save_input(content, *, settings=()):
            message = agent.make(m.UserMessageRecord, payload={"content": content})
            await history.append(
                sid, agent.records.group([message, *settings], message.operation_id)
            )

        await save_input(
            [m.TextPart(text="First waiting")],
            settings=[
                agent.make(
                    m.SamplingRecord,
                    scope="request",
                    payload={"value": {"temperature": 0.1}},
                ),
                agent.make(
                    m.InferenceRecord,
                    scope="request",
                    payload={"value": {"max_completion_tokens": 100}},
                ),
            ],
        )
        await save_input(
            [m.TextPart(text="Second waiting")],
            settings=[
                agent.make(
                    m.SamplingRecord,
                    scope="request",
                    payload={"value": {"temperature": 0.4}},
                ),
            ],
        )
        settings = []
        if last_override == "value":
            settings.append(
                agent.make(
                    m.SamplingRecord,
                    scope="request",
                    payload={"value": {"temperature": 0.7}},
                )
            )
        if last_override == "remove":
            settings.append(agent.make(m.SamplingRemovedRecord, scope="request"))

        await save_input([m.TextPart(text="Last waiting")], settings=settings)
        waiting = await history.pending_messages(sid)
        assert len(waiting) == 3
        selected = await agent._prepare(continue_tools=False)
        assert selected is not None
        assert selected.operation_id == waiting[-1].operation_id
        await agent.cancel()
        agent.paused = False
        agent.wake()
        async with asyncio.timeout(agent_timeout):
            while len(llm.requests) < 2:
                await asyncio.sleep(0.01)

        sent = llm.requests[1]
        tool.release.set()
        async with asyncio.timeout(agent_timeout):
            await agent.idle.wait()

        assert agent.last_error is None
        assert len(llm.requests) >= 2
        assert sent["max_completion_tokens"] == 400
        if last_override == "value":
            assert sent["temperature"] == 0.7
        elif last_override == "absent":
            assert sent["temperature"] == 0.8
        else:
            assert "temperature" not in sent

        assert [part["content"][0]["text"] for part in sent["messages"][-3:]] == [
            "First waiting",
            "Second waiting",
            "Last waiting",
        ]
        replay = await history.openai(
            sid, selected.operation_id, through_seq=selected.through_seq
        )
        # A historical cut made while the tool ran keeps pending; the later call includes completion.
        assert replay.request["messages"][2]["role"] == "tool"
        assert any(
            status in str(replay.request["messages"][2]["content"])
            for status in ("pending", "running")
        )
        assert not await history.pending_messages(sid)
    finally:
        tool.release.set()
        await agent.close()
        async with history.pool.connection() as connection:
            await connection.execute(
                "delete from boba.records where session_id = %s", (sid,)
            )
            await connection.execute(
                "delete from boba.sessions where session_id = %s", (sid,)
            )
