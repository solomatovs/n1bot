"""Миграция истории на стенде Postgres: синтетический тред со всеми ветками
соответствия, копия тредов базы чата стенда, вход скрипта с файлом настроек.
Источник стенда только читается; копии и целевые схемы живут в тестовой базе
и сносятся после теста. Воспроизведение проверяется загрузкой мигрированной
сессии ядром агента (HistoryLoader → Session)."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from omegaconf import DictConfig, OmegaConf
from psycopg import sql
from psycopg.types.json import Jsonb

from boba.agent.history import SessionId
from boba.agent.ids import IdMint, SystemClock
from boba.agent.loader import HistoryLoader, LoadReport
from boba.agent.records import (
    AssistantRecord,
    AttachmentRecord,
    ChainRecord,
    OriginKind,
    Synthetic,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserRecord,
)
from boba.agent.session import Session
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.history.postgres import PostgresHistoryStore
from boba.history_migrate.app import (
    HistoryMigrateCli,
    HistoryMigration,
    MigrationArgs,
    MigrationError,
    MigrationReport,
    ThreadOutcome,
)
from boba.history_migrate.source import CheckpointSource
from boba.history_migrate.translate import (
    MigratedAttachment,
    RecordTranslator,
    ToolUseResultKey,
)
from boba.stand.database import TestDatabase

pytestmark = pytest.mark.anyio

VERSION = "0.0.26.dev4"
CHECKPOINTER_POSTGRES = "checkpointer.postgres"
CHECKPOINTER_SCHEMA = "checkpointer.schema"
DATA_LAYER_SCHEMA = "data_layer.schema"
KRB_CONFIG = "krb.config"
MESSAGES = "messages"
INTERRUPTED = AgentTemplate().read(AgentTemplateFile.REQUEST_INTERRUPTED)

# то же, что ChatView.NAMESPACE и роли StepRole чата: по ним выведены
# elements.for_id; здесь только для подсчёта покрытия, инвариант теста —
# равенство множеств ключей
STEP_NAMESPACE = UUID("6f9b1f4e-2f1a-4c1a-9a2f-1d3b5c7e9a11")
STEP_ROLES = (
    "answer",
    "error",
    "process",
    "pulse",
    "stage",
    "thinking",
    "tool",
    "chart",
    "element",
    "stream",
)
COPY_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
STAND_THREADS = 16


@pytest.fixture
async def postgres(raw_config: DictConfig) -> PostgresConfig:
    cfg = bind(raw_config, path="postgres", model=PostgresConfig)
    database = await TestDatabase.ensure(cfg)

    return TestDatabase.config_of(cfg, database)


@pytest.fixture
def source_postgres(raw_config: DictConfig) -> PostgresConfig:
    """База чата стенда: источник, который только читается."""
    return bind(raw_config, path=CHECKPOINTER_POSTGRES, model=PostgresConfig)


@pytest.fixture
def source_schema(raw_config: DictConfig) -> str:
    return str(OmegaConf.select(raw_config, CHECKPOINTER_SCHEMA))


@pytest.fixture
def chainlit_schema(raw_config: DictConfig) -> str:
    return str(OmegaConf.select(raw_config, DATA_LAYER_SCHEMA))


class Schemas:
    """Две схемы теста в тестовой базе: копия checkpoints и история агента."""

    def __init__(self) -> None:
        tag = uuid.uuid4().hex[:10]
        self.checkpoints = f"hm_cp_{tag}"
        self.history = f"hm_hist_{tag}"

    async def drop(self, pool: AsyncPostgresPool) -> None:
        async with pool.connection() as conn:
            for name in (self.checkpoints, self.history):
                await conn.execute(
                    sql.SQL("drop schema if exists {} cascade").format(
                        sql.Identifier(name)
                    )
                )


@pytest.fixture
async def schemas(postgres: PostgresConfig) -> AsyncIterator[Schemas]:
    names = Schemas()
    yield names
    pool = AsyncPostgresPool(postgres)
    await pool.open()
    try:
        await names.drop(pool)
    finally:
        await pool.close()


@pytest.fixture
async def copy_pool(
    postgres: PostgresConfig, schemas: Schemas
) -> AsyncIterator[AsyncPostgresPool]:
    """Пул копии checkpoints: search_path на её схему, таблицы langgraph созданы."""
    pool = AsyncPostgresPool(postgres.with_schema(schemas.checkpoints))
    await pool.open()
    try:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("create schema if not exists {}").format(
                    sql.Identifier(schemas.checkpoints)
                )
            )
        await AsyncPostgresSaver(pool.raw).setup()
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def history_pool(postgres: PostgresConfig) -> AsyncIterator[AsyncPostgresPool]:
    pool = AsyncPostgresPool(postgres)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


class Stand:
    """Собранная миграция на схемах теста плюс чтение итога ядром; создаётся
    в цикле событий теста: чекпойнтер langgraph берёт текущий loop."""

    def __init__(
        self,
        postgres: PostgresConfig,
        schemas: Schemas,
        copy_pool: AsyncPostgresPool,
        history_pool: AsyncPostgresPool,
    ) -> None:
        self.postgres = postgres
        self.schemas = schemas
        self.copy_pool = copy_pool
        self.history_pool = history_pool
        self.source = CheckpointSource(copy_pool, schemas.checkpoints)

    def store(self) -> PostgresHistoryStore:
        return PostgresHistoryStore(
            self.postgres, self.schemas.history, self.history_pool
        )

    def migration(self) -> HistoryMigration:
        translator = RecordTranslator(IdMint(), AgentTemplate(), SystemClock(), VERSION)

        return HistoryMigration(self.source, self.store(), translator)

    async def loaded(self, thread_id: str) -> tuple[Session, LoadReport]:
        session = Session(SessionId(value=thread_id))
        loader = HistoryLoader(
            self.store(), AgentTemplate(), IdMint(), SystemClock(), VERSION
        )
        report = await loader.load(session)

        return session, report


@pytest.fixture
async def stand(
    postgres: PostgresConfig,
    schemas: Schemas,
    copy_pool: AsyncPostgresPool,
    history_pool: AsyncPostgresPool,
) -> Stand:
    return Stand(postgres, schemas, copy_pool, history_pool)


class ThreadWriter:
    """Пишет тред в копию checkpoints штатным чекпойнтером: последний
    checkpoint с каналом messages и, по желанию, ожидающие записи."""

    def __init__(self, pool: AsyncPostgresPool) -> None:
        self._saver = AsyncPostgresSaver(pool.raw)

    async def write(
        self,
        thread_id: str,
        messages: Sequence[BaseMessage],
        pending: Sequence[BaseMessage] = (),
    ) -> None:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {MESSAGES: list(messages)}
        checkpoint["channel_versions"] = {MESSAGES: 1}
        config = RunnableConfig(
            configurable={"thread_id": thread_id, "checkpoint_ns": ""}
        )
        metadata = CheckpointMetadata(source="loop", step=1, parents={})
        saved = await self._saver.aput(config, checkpoint, metadata, {MESSAGES: 1})
        if not pending:
            return

        await self._saver.aput_writes(saved, [(MESSAGES, list(pending))], "task-1")


class SourceCopy:
    """Копирует треды базы чата стенда в схему копии; источник только читается."""

    def __init__(
        self,
        source_postgres: PostgresConfig,
        source_schema: str,
        target_pool: AsyncPostgresPool,
        target_schema: str,
    ) -> None:
        self._postgres = source_postgres.with_schema(source_schema)
        self._schema = source_schema
        self._target_pool = target_pool
        self._target_schema = target_schema

    async def pick_and_copy(self, limit: int) -> dict[str, set[str]]:
        """Выбирает треды с разными особенностями, копирует их, отдаёт
        особенности по тредам."""
        pool = AsyncPostgresPool(self._postgres)
        await pool.open()
        try:
            chosen = await self._pick(CheckpointSource(pool, self._schema), limit)
            await self._copy(pool, list(chosen))
        finally:
            await pool.close()

        return chosen

    async def _pick(self, source: CheckpointSource, limit: int) -> dict[str, set[str]]:
        chosen: dict[str, set[str]] = {}
        covered: set[str] = set()
        for thread_id in await source.thread_ids():
            transcript = await source.transcript(thread_id)
            if transcript is None:
                continue

            features = set(self._features(transcript.messages))
            fresh = features - covered
            plain = not features and len(chosen) < limit // 2
            if not fresh and not plain:
                continue

            chosen[thread_id] = features
            covered |= features
            if len(chosen) >= limit:
                break

        return chosen

    @staticmethod
    def _features(messages: Sequence[BaseMessage]) -> Iterator[str]:
        for message in messages:
            extra = message.additional_kwargs
            if isinstance(message, HumanMessage) and extra.get("attachments"):
                yield "attachments"

            if isinstance(message, AIMessage):
                for key in ("stopped", "error", "prefetch_elapsed_ms"):
                    if key in extra:
                        yield key

                if message.tool_calls and message.content:
                    yield "calls_with_text"

            if isinstance(message, ToolMessage):
                for key in ("boba_journal", "boba_call_media"):
                    if key in message.response_metadata:
                        yield key

                if message.status == "error":
                    yield "tool_error"

                if message.artifact is None:
                    yield "tool_without_artifact"

    async def _copy(self, pool: AsyncPostgresPool, threads: Sequence[str]) -> None:
        for table in COPY_TABLES:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL("select * from {}.{} where thread_id = any(%s)").format(
                        sql.Identifier(self._schema), sql.Identifier(table)
                    ),
                    (list(threads),),
                )
                assert cur.description is not None
                columns = [column.name for column in cur.description]
                rows = await cur.fetchall()

            await self._insert(table, columns, rows)

    async def _insert(
        self, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]
    ) -> None:
        if not rows:
            return

        statement = sql.SQL("insert into {}.{} ({}) values ({})").format(
            sql.Identifier(self._target_schema),
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )
        prepared: list[list[Any]] = []
        for row in rows:
            prepared.append([self._value(value) for value in row])

        async with self._target_pool.connection() as conn:
            await conn.cursor().executemany(statement, prepared)

    @staticmethod
    def _value(value: Any) -> Any:
        if isinstance(value, dict | list):
            return Jsonb(value)

        return value


class Events:
    """Беседа как список событий: из сообщений источника и из записей ядра —
    одинаковой формы, чтобы сравнивать порядок и содержимое."""

    def of_messages(self, messages: Sequence[BaseMessage]) -> list[tuple[Any, ...]]:
        events: list[tuple[Any, ...]] = []
        for message in messages:
            events.extend(self._of_message(message))

        return events

    def _of_message(self, message: BaseMessage) -> Iterator[tuple[Any, ...]]:
        text = self._text(message)
        if isinstance(message, HumanMessage):
            yield ("user", message.id, text)
            return

        if isinstance(message, ToolMessage):
            yield ("tool", message.tool_call_id, text, message.status == "error")
            return

        if not isinstance(message, AIMessage):
            return

        extra = message.additional_kwargs
        reasoning = str(extra.get("reasoning_content") or "")
        if extra.get("error"):
            yield ("api_error", message.id, text)
            return

        calls = self._calls(message)
        if extra.get("stopped"):
            if reasoning or text or calls:
                yield ("aborted", message.id, reasoning, text, calls)

            yield ("interrupted",)
            return

        if not reasoning and not text and not calls:
            return

        yield ("assistant", message.id, reasoning, text, calls)

    @staticmethod
    def _calls(message: AIMessage) -> tuple[tuple[str, str, str], ...]:
        calls: list[tuple[str, str, str]] = []
        for call in message.tool_calls:
            if not call.get("id") or not call.get("name"):
                continue

            calls.append(
                (
                    str(call["id"]),
                    str(call["name"]),
                    json.dumps(dict(call["args"]), sort_keys=True),
                )
            )

        return tuple(calls)

    @staticmethod
    def _text(message: BaseMessage) -> str:
        content = message.content
        if isinstance(content, str):
            return content

        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif block.get("type") == "text":
                parts.append(str(block.get("text", "")))

        return "".join(parts)

    def of_records(self, records: Sequence[ChainRecord]) -> list[tuple[Any, ...]]:
        events: list[tuple[Any, ...]] = []
        group: list[AssistantRecord] = []
        for record in records:
            if isinstance(record, AssistantRecord):
                if group and group[-1].message.id != record.message.id:
                    events.append(self._of_group(group))
                    group = []

                group.append(record)
                continue

            if group:
                events.append(self._of_group(group))
                group = []

            events.extend(self._of_user(record))

        if group:
            events.append(self._of_group(group))

        return events

    @staticmethod
    def _of_user(record: ChainRecord) -> Iterator[tuple[Any, ...]]:
        if not isinstance(record, UserRecord):
            return

        content = record.message.content
        if isinstance(content, str):
            if record.origin is not None and record.origin.kind is OriginKind.HUMAN:
                yield ("user", record.uuid, content)
                return

            if content == INTERRUPTED:
                yield ("interrupted",)
                return

            yield ("meta", content)
            return

        for result in record.message.tool_results():
            yield ("tool", result.tool_use_id, result.text(), bool(result.is_error))

    @staticmethod
    def _of_group(group: Sequence[AssistantRecord]) -> tuple[Any, ...]:
        first = group[0]
        if first.is_api_error_message:
            return ("api_error", first.message.id, first.message.content[0].text)  # type: ignore[union-attr]

        reasoning: list[str] = []
        texts: list[str] = []
        calls: list[tuple[str, str, str]] = []
        for record in group:
            for block in record.message.content:
                if isinstance(block, ThinkingBlock):
                    reasoning.append(block.thinking)
                elif isinstance(block, TextBlock):
                    texts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    calls.append(
                        (
                            block.id,
                            block.name,
                            json.dumps(dict(block.input), sort_keys=True),
                        )
                    )

        kind = "assistant"
        if first.is_aborted_mid_stream:
            kind = "aborted"

        return (
            kind,
            first.message.id,
            "".join(reasoning),
            "".join(texts),
            tuple(calls),
        )


class Keys:
    """Ключи привязки элементов: id вопроса, id ответа, id вызова."""

    def of_messages(self, messages: Sequence[BaseMessage]) -> set[str]:
        keys: set[str] = set()
        for message in messages:
            if isinstance(message, HumanMessage | AIMessage) and message.id:
                keys.add(message.id)

            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    if call.get("id"):
                        keys.add(str(call["id"]))

        return keys

    def of_records(self, records: Sequence[ChainRecord]) -> set[str]:
        keys: set[str] = set()
        for record in records:
            if isinstance(record, UserRecord) and record.origin is not None:
                keys.add(record.uuid)

            if isinstance(record, AssistantRecord):
                keys.add(record.message.id)
                for block in record.message.tool_uses():
                    keys.add(block.id)

        return keys

    def derived(self, thread_id: str, keys: set[str]) -> set[str]:
        ids: set[str] = set()
        for key in keys:
            for role in STEP_ROLES:
                ids.add(str(uuid5(STEP_NAMESPACE, f"{thread_id}/{key}/{role}")))

        return ids


def stored(session: Session, report: LoadReport) -> list[ChainRecord]:
    """Записи цепочки, которые лежали в хранилище: без починки загрузчика."""
    kept: list[ChainRecord] = []
    for record in session.chain():
        if record.seq <= report.stored_seq:
            kept.append(record)

    return kept


class SyntheticThread:
    """Тред со всеми ветками соответствия; id сообщений — как у chainlit."""

    def __init__(self) -> None:
        self.thread_id = str(uuid.uuid4())
        self.question_id = str(uuid.uuid4())
        self.answer_id = "run--" + str(uuid.uuid4())
        self.prefetch_id = "run--" + str(uuid.uuid4())
        self.final_id = "run--" + str(uuid.uuid4())
        self.stopped_id = "run--" + str(uuid.uuid4())
        self.error_id = "run--" + str(uuid.uuid4())
        self.second_question_id = str(uuid.uuid4())
        self.journal = {
            "server": "mcp-main",
            "call_id": "call_abc",
            "channel": "tool_log",
        }
        self.media = {"call_abc": {"query": "text/x-sql"}}
        self.artifact = {"kind": "table", "columns": ["a"], "rows": [[1]]}

    def messages(self) -> list[BaseMessage]:
        prefetch = AIMessage(
            content="",
            id=self.prefetch_id,
            tool_calls=[
                {
                    "id": "prefetch-1",
                    "name": "kb_search",
                    "args": {"q": "x"},
                    "type": "tool_call",
                }
            ],
            additional_kwargs={"reasoning_content": "", "prefetch_elapsed_ms": 120},
        )
        prefetch_result = ToolMessage(
            content="found 2 docs",
            tool_call_id="prefetch-1",
            name="kb_search",
            status="success",
        )
        answer = AIMessage(
            content="Running the query.",
            id=self.answer_id,
            tool_calls=[
                {
                    "id": "call_abc",
                    "name": "pg_query",
                    "args": {"query": "select 1"},
                    "type": "tool_call",
                },
                {
                    "id": "call_def",
                    "name": "shell",
                    "args": {"cmd": "ls"},
                    "type": "tool_call",
                },
            ],
            additional_kwargs={"reasoning_content": "Think first."},
            response_metadata={
                "model_name": "stand-model",
                "finish_reason": "tool_calls",
            },
            usage_metadata=UsageMetadata(
                input_tokens=11, output_tokens=7, total_tokens=18
            ),
        )
        result_ok = ToolMessage(
            content="a\n1",
            tool_call_id="call_abc",
            name="pg_query",
            status="success",
            artifact=self.artifact,
        )
        result_ok.response_metadata["boba_journal"] = self.journal
        result_ok.response_metadata["boba_call_media"] = self.media
        result_err = ToolMessage(
            content="ls: boom", tool_call_id="call_def", name="shell", status="error"
        )
        final = AIMessage(
            content="Done: 1 row.",
            id=self.final_id,
            response_metadata={"model_name": "stand-model", "finish_reason": "stop"},
            usage_metadata=UsageMetadata(
                input_tokens=30, output_tokens=5, total_tokens=35
            ),
        )
        stopped = AIMessage(
            content="_stopped by the user_",
            id=self.stopped_id,
            additional_kwargs={"stopped": True, "reasoning_content": "half a thought"},
        )
        error = AIMessage(
            content="model unavailable: 503",
            id=self.error_id,
            additional_kwargs={"error": True},
        )
        return [
            HumanMessage(
                content="How many rows?",
                id=self.question_id,
                additional_kwargs={
                    "attachments": [{"name": "data.csv", "path": "/work/data.csv"}]
                },
            ),
            prefetch,
            prefetch_result,
            answer,
            result_ok,
            result_err,
            final,
            HumanMessage(content="Again, please", id=self.second_question_id),
            stopped,
            error,
        ]

    def pending(self) -> list[BaseMessage]:
        """Итог инструмента оборванного хода: лежит только в ожидающих записях."""
        return [
            ToolMessage(
                content="late result",
                tool_call_id="call_late",
                name="shell",
                status="success",
            )
        ]


class TestSyntheticThread:
    async def test_every_branch_round_trips_through_the_core(
        self, stand: Stand
    ) -> None:
        thread = SyntheticThread()
        messages = thread.messages()
        await ThreadWriter(stand.copy_pool).write(
            thread.thread_id, messages, pending=thread.pending()
        )

        report = await stand.migration().run([thread.thread_id])
        print(report.render())
        [outcome] = report.threads
        assert outcome.outcome is ThreadOutcome.MIGRATED, outcome
        assert outcome.skipped == 0
        assert outcome.notes == [
            "tool result call_late answers a call this thread does not record"
        ]

        session, load = await stand.loaded(thread.thread_id)
        assert load.skipped_lines == 0
        assert load.skipped_records == 0
        records = stored(session, load)

        expected = Events().of_messages([*messages, *thread.pending()])
        assert Events().of_records(records) == expected
        assert Keys().of_records(records) == Keys().of_messages(messages)

        users = [r for r in records if isinstance(r, UserRecord)]
        assert users[0].uuid == thread.question_id
        assert users[0].message.content == "How many rows?"
        attachments = [r for r in records if isinstance(r, AttachmentRecord)]
        assert len(attachments) == 1
        assert attachments[0].parent_uuid == thread.question_id
        assert attachments[0].attachment.type == MigratedAttachment.FILES.value
        assert attachments[0].rendered is not None
        assert "/work/data.csv" in attachments[0].rendered[0].content

        again = await stand.migration().run([thread.thread_id])
        assert again.threads[0].outcome is ThreadOutcome.PRESENT
        session_again, load_again = await stand.loaded(thread.thread_id)
        assert load_again.stored_seq == load.stored_seq
        assert stored(session_again, load_again) == records

    async def test_record_fields_follow_the_agent_journal(self, stand: Stand) -> None:
        thread = SyntheticThread()
        await ThreadWriter(stand.copy_pool).write(
            thread.thread_id, thread.messages(), pending=thread.pending()
        )
        await stand.migration().run([thread.thread_id])
        session, load = await stand.loaded(thread.thread_id)
        records = stored(session, load)
        users = [r for r in records if isinstance(r, UserRecord)]

        assistants = [r for r in records if isinstance(r, AssistantRecord)]
        by_id: dict[str, list[AssistantRecord]] = {}
        for record in assistants:
            by_id.setdefault(record.message.id, []).append(record)

        assert [r.api_block_index for r in by_id[thread.answer_id]] == [0, 1, 2, 3]
        assert {r.request_id for r in by_id[thread.answer_id]} == {
            by_id[thread.answer_id][0].request_id
        }
        assert by_id[thread.answer_id][0].message.model == "stand-model"
        assert by_id[thread.answer_id][0].message.usage.input_tokens == 11
        assert by_id[thread.answer_id][0].message.stop_reason.value == "tool_use"
        assert by_id[thread.prefetch_id][0].message.model == Synthetic.MODEL.value
        assert by_id[thread.final_id][0].message.stop_reason.value == "end_turn"
        assert by_id[thread.stopped_id][0].is_aborted_mid_stream is True
        assert by_id[thread.error_id][0].is_api_error_message is True
        assert by_id[thread.error_id][0].error == "model unavailable: 503"

        results = {
            r.message.tool_results()[0].tool_use_id: r
            for r in users
            if r.message.tool_results()
        }
        ok = results["call_abc"]
        assert ok.source_tool_assistant_uuid == by_id[thread.answer_id][2].uuid
        assert ok.tool_use_result == {
            ToolUseResultKey.ARTIFACT.value: thread.artifact,
            ToolUseResultKey.JOURNAL.value: thread.journal,
            ToolUseResultKey.MEDIA.value: thread.media,
        }
        assert results["call_def"].message.tool_results()[0].is_error is True
        assert results["call_def"].tool_use_result is None
        assert results["call_late"].source_tool_assistant_uuid is None
        stamps = {r.timestamp for r in records}
        assert len(stamps) == 1

    async def test_unfit_thread_id_is_reported_and_others_proceed(
        self, stand: Stand
    ) -> None:
        writer = ThreadWriter(stand.copy_pool)
        good = SyntheticThread()
        await writer.write(good.thread_id, good.messages())
        await writer.write(
            "bad thread!", [HumanMessage(content="hi", id=str(uuid.uuid4()))]
        )

        report = await stand.migration().run(["bad thread!", good.thread_id, "missing"])
        by_thread = {t.thread_id: t for t in report.threads}
        assert by_thread["bad thread!"].outcome is ThreadOutcome.FAILED
        assert "does not fit SessionId" in by_thread["bad thread!"].error
        assert by_thread[good.thread_id].outcome is ThreadOutcome.MIGRATED
        assert by_thread["missing"].outcome is ThreadOutcome.EMPTY
        assert "failed threads:" in report.render()


class TestStandCopy:
    async def test_copied_threads_reproduce_the_conversation(
        self,
        stand: Stand,
        source_postgres: PostgresConfig,
        source_schema: str,
        chainlit_schema: str,
    ) -> None:
        copy = SourceCopy(
            source_postgres, source_schema, stand.copy_pool, stand.schemas.checkpoints
        )
        chosen = await copy.pick_and_copy(STAND_THREADS)
        assert chosen, "the stand chat database holds no threads to migrate"
        print("features:", {t: sorted(f) for t, f in chosen.items()})

        report = await stand.migration().run(list(chosen))
        print(report.render())
        assert not report.of_outcome(ThreadOutcome.FAILED)
        assert len(report.of_outcome(ThreadOutcome.MIGRATED)) == len(chosen)

        bound = await self._elements(source_postgres, chainlit_schema, list(chosen))
        matched_old = 0
        matched_new = 0
        for thread_id in chosen:
            transcript = await stand.source.transcript(thread_id)
            assert transcript is not None
            session, load = await stand.loaded(thread_id)
            assert load.skipped_lines == 0, thread_id
            assert load.skipped_records == 0, thread_id
            records = stored(session, load)

            assert Events().of_records(records) == Events().of_messages(
                transcript.messages
            ), thread_id

            old_keys = Keys().of_messages(transcript.messages)
            new_keys = Keys().of_records(records)
            assert new_keys == old_keys, thread_id
            element_ids = bound.get(thread_id, set())
            matched_old += len(element_ids & Keys().derived(thread_id, old_keys))
            matched_new += len(element_ids & Keys().derived(thread_id, new_keys))

        assert matched_new == matched_old
        total = sum(len(v) for v in bound.values())
        print(f"elements bound to preserved keys: {matched_new} of {total}")

    @staticmethod
    async def _elements(
        postgres: PostgresConfig, schema: str, threads: Sequence[str]
    ) -> dict[str, set[str]]:
        """for_id и id элементов chainlit выбранных тредов; источник только читается."""
        pool = AsyncPostgresPool(postgres)
        await pool.open()
        try:
            async with pool.cursor() as cur:
                await cur.execute(
                    sql.SQL(
                        "select thread_id::text, for_id::text, id::text "
                        "from {}.elements where thread_id::text = any(%s)"
                    ).format(sql.Identifier(schema)),
                    (list(threads),),
                )
                rows = await cur.fetchall()
        finally:
            await pool.close()

        bound: dict[str, set[str]] = {}
        for thread_id, for_id, element_id in rows:
            ids = bound.setdefault(thread_id, set())
            ids.add(for_id)
            ids.add(element_id)

        return bound


class TomlText:
    """Минимальный toml из словаря моделей: таблицы — вложенные словари,
    значения — литералы JSON, которые toml принимает (str, int, float, bool, list)."""

    def render(self, data: Mapping[str, Any]) -> str:
        lines: list[str] = []
        self._table(data, (), lines)

        return "\n".join(lines) + "\n"

    def _table(
        self, data: Mapping[str, Any], path: tuple[str, ...], lines: list[str]
    ) -> None:
        if path:
            lines.append("[" + ".".join(path) + "]")

        nested: list[tuple[str, Mapping[str, Any]]] = []
        for key, value in data.items():
            if isinstance(value, Mapping):
                nested.append((key, value))
                continue

            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")

        for key, value in nested:
            lines.append("")
            self._table(value, (*path, key), lines)


class TestCli:
    def settings_text(
        self, stand: Stand, raw_config: DictConfig, tmp_path: Path
    ) -> str:
        postgres = stand.postgres.model_dump(mode="json", exclude_none=True)
        data = {
            "source": {"postgres": postgres, "schema": stand.schemas.checkpoints},
            "target": {"postgres": postgres, "schema": stand.schemas.history},
            "krb": {
                "config": str(OmegaConf.select(raw_config, KRB_CONFIG)),
                "ccache_dir": str(tmp_path / "krb"),
            },
        }

        return TomlText().render(data)

    async def test_settings_file_drives_the_run(
        self, stand: Stand, raw_config: DictConfig, tmp_path: Path
    ) -> None:
        thread = SyntheticThread()
        await ThreadWriter(stand.copy_pool).write(thread.thread_id, thread.messages())
        settings = tmp_path / "migrate.toml"
        settings.write_text(
            self.settings_text(stand, raw_config, tmp_path), encoding="utf-8"
        )
        os.chmod(settings, 0o600)
        report_path = tmp_path / "report.json"

        command = HistoryMigrateCli()
        args = command.parse(
            [
                "--settings",
                str(settings),
                "--report",
                str(report_path),
                "--thread",
                thread.thread_id,
            ]
        )
        assert args == MigrationArgs(
            settings=settings, report=report_path, threads=[thread.thread_id]
        )
        report = await command.run(args)
        assert [t.outcome for t in report.threads] == [ThreadOutcome.MIGRATED]
        written = MigrationReport.model_validate_json(
            report_path.read_text(encoding="utf-8")
        )
        assert written == report

        session, load = await stand.loaded(thread.thread_id)
        assert load.stored_seq == report.threads[0].records
        assert Events().of_records(stored(session, load)) == Events().of_messages(
            thread.messages()
        )

        again = await command.run(command.parse(["--settings", str(settings)]))
        assert [t.outcome for t in again.threads] == [ThreadOutcome.PRESENT]

    async def test_shared_settings_file_is_refused(
        self, stand: Stand, raw_config: DictConfig, tmp_path: Path
    ) -> None:
        settings = tmp_path / "migrate.toml"
        settings.write_text(
            self.settings_text(stand, raw_config, tmp_path), encoding="utf-8"
        )
        os.chmod(settings, 0o644)

        command = HistoryMigrateCli()
        with pytest.raises(MigrationError, match="mode 0600"):
            await command.run(command.parse(["--settings", str(settings)]))

        broken = tmp_path / "broken.toml"
        broken.write_text('[source]\nschema = "x"\n', encoding="utf-8")
        os.chmod(broken, 0o600)
        with pytest.raises(MigrationError, match=r"\[source\] and \[target\]"):
            await command.run(command.parse(["--settings", str(broken)]))
