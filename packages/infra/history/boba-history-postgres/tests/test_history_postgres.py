"""Хранилище истории в Postgres на настоящей базе стенда: контракт порта
(повтор пачки без дублей, занятая сессия, откат пачки целиком, порядок
чтения), агент на стенде модели с этим хранилищем и возобновление, обрыв
на каждой записи даёт тот же восстановленный список, что JSONL;
возобновление журнала в десятки мегабайт — под меткой integration."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from omegaconf import DictConfig
from psycopg import sql

from boba.agent.control import AllowAnswer
from boba.agent.history import HistoryError, HistoryStore, SessionId, StoredLine
from boba.agent.ids import IdMint, SystemClock
from boba.agent.loader import HistoryLoader, LoadReport
from boba.agent.queue import QueueEntry, QueueKind
from boba.agent.records import (
    AssistantMessage,
    AssistantRecord,
    AttachmentRecord,
    ChainRecord,
    CompactBoundaryRecord,
    PermissionBehavior,
    PermissionMode,
    PermissionRule,
    Record,
    RecordCodec,
    ReplyStopReason,
    TextBlock,
    Usage,
    UserMessage,
    UserRecord,
)
from boba.agent.session import Session
from boba.agent.template import AgentTemplate
from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.history.jsonl import JsonlHistoryStore
from boba.history.postgres import PostgresHistoryStore
from boba.stand.agent import (
    Collected,
    FakeServer,
    Protocol,
    Served,
    StandAgents,
    StandToolName,
    StandTools,
)
from boba.stand.database import TestDatabase
from boba.stand.ui.fake_llm import ScenarioName

pytestmark = pytest.mark.anyio

SESSION = SessionId(value="history-pg-1")
VERSION = "0.0.26.dev4"
STAMP = "2026-10-10T12:00:00.000Z"


def prompt(text: str) -> QueueEntry:
    return QueueEntry(kind=QueueKind.PROMPT, text=text)


def kinds(records: Sequence[Record] | Sequence[ChainRecord]) -> list[str]:
    names: list[str] = []
    for record in records:
        name = record.type.value
        if isinstance(record, AttachmentRecord):
            name = f"attachment:{record.attachment.type}"

        names.append(name)

    return names


class Journal:
    """Записи сессии для контрактных тестов: пары user/assistant с seq."""

    def __init__(self, session_id: SessionId) -> None:
        self._session = Session(session_id)
        self._ids = IdMint()

    def user(self, text: str, tool_use_result: object = None) -> UserRecord:
        return self._session.add(
            UserRecord(
                uuid=self._ids.uuid(),
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=STAMP,
                session_id=self._session.id.value,
                version=VERSION,
                message=UserMessage(content=text),
                tool_use_result=tool_use_result,
            )
        )

    def assistant(self, text: str) -> AssistantRecord:
        return self._session.add(
            AssistantRecord(
                uuid=self._ids.uuid(),
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=STAMP,
                session_id=self._session.id.value,
                version=VERSION,
                message=AssistantMessage(
                    id=self._ids.message_id(),
                    model="fake-model",
                    content=[TextBlock(text=text)],
                    stop_reason=ReplyStopReason.END_TURN,
                    usage=Usage(input_tokens=10, output_tokens=5),
                ),
                request_id=self._ids.request_id(),
            )
        )

    def records(self) -> Sequence[Record]:
        return self._session.records()


async def loaded(
    store: HistoryStore, session_id: SessionId
) -> tuple[Session, LoadReport]:
    session = Session(session_id)
    loader = HistoryLoader(store, AgentTemplate(), IdMint(), SystemClock(), VERSION)
    report = await loader.load(session)

    return session, report


async def read_all(
    store: HistoryStore, session_id: SessionId
) -> list[dict[str, object]]:
    documents: list[dict[str, object]] = []
    async for line in store.read(session_id):
        assert isinstance(line, StoredLine), line
        documents.append(dict(line.raw))

    return documents


@pytest.fixture
async def postgres(raw_config: DictConfig) -> PostgresConfig:
    cfg = bind(raw_config, path="postgres", model=PostgresConfig)
    database = await TestDatabase.ensure(cfg)

    return TestDatabase.config_of(cfg, database)


@pytest.fixture
async def pool(postgres: PostgresConfig) -> AsyncIterator[AsyncPostgresPool]:
    """Пул на тест: у каждого теста свой цикл событий, кэш пулов процесса
    привязан к первому из них."""
    opened = AsyncPostgresPool(postgres)
    await opened.open()
    try:
        yield opened
    finally:
        await opened.close()


@pytest.fixture
async def schema(pool: AsyncPostgresPool) -> AsyncIterator[str]:
    """Своя схема на тест; после теста сносится."""
    name = f"hist_{uuid.uuid4().hex[:12]}"
    yield name
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name))
        )


class Stores:
    """Хранилища одной схемы на одном пуле: второй экземпляр — «другой процесс»."""

    def __init__(
        self, postgres: PostgresConfig, schema: str, pool: AsyncPostgresPool
    ) -> None:
        self._postgres = postgres
        self._schema = schema
        self._pool = pool

    def new(self) -> PostgresHistoryStore:
        return PostgresHistoryStore(self._postgres, self._schema, self._pool)


@pytest.fixture
def stores(postgres: PostgresConfig, schema: str, pool: AsyncPostgresPool) -> Stores:
    return Stores(postgres, schema, pool)


@pytest.fixture
def store(stores: Stores) -> PostgresHistoryStore:
    return stores.new()


@pytest.fixture
async def server() -> AsyncIterator[FakeServer]:
    fake = FakeServer()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def agents(server: FakeServer, tmp_path: Path) -> AsyncIterator[StandAgents]:
    stand = StandAgents(server, tmp_path / "history")
    try:
        yield stand
    finally:
        await stand.aclose()


class TestContract:
    async def test_repeated_batches_keep_the_order_without_duplicates(
        self, store: PostgresHistoryStore
    ) -> None:
        journal = Journal(SESSION)
        journal.user("one")
        journal.assistant("two")
        journal.user("three")
        first = list(journal.records())
        journal.assistant("four")
        journal.user("five")
        everything = list(journal.records())

        lease = await store.acquire(SESSION)
        try:
            await store.append(lease, first)
            await store.append(lease, first)
            await store.append(lease, everything[1:])
        finally:
            await store.release(lease)

        documents = await read_all(store, SESSION)
        assert [d["seq"] for d in documents] == [1, 2, 3, 4, 5]
        assert documents == [json.loads(RecordCodec().line(r)) for r in everything]

        session, report = await loaded(store, SESSION)
        assert report.stored_seq == 5
        assert list(session.records())[:5] == everything

    async def test_second_process_on_the_session_is_refused(
        self, store: PostgresHistoryStore, stores: Stores
    ) -> None:
        lease = await store.acquire(SESSION)
        other = stores.new()
        try:
            with pytest.raises(HistoryError, match="already in use"):
                await other.acquire(SESSION)
        finally:
            await store.release(lease)

        again = await other.acquire(SESSION)
        await other.release(again)

    async def test_failed_batch_leaves_no_record(
        self, store: PostgresHistoryStore
    ) -> None:
        """Символ U+0000 в строке jsonb не принимает: вторая запись пачки
        роняет транзакцию, первая не остаётся."""
        journal = Journal(SESSION)
        journal.user("fine")
        journal.user("broken \x00 text")
        broken = list(journal.records())

        lease = await store.acquire(SESSION)
        try:
            with pytest.raises(HistoryError, match="appending 2 record"):
                await store.append(lease, broken)

            assert await read_all(store, SESSION) == []

            await store.append(lease, broken[:1])
        finally:
            await store.release(lease)

        assert [d["seq"] for d in await read_all(store, SESSION)] == [1]

    async def test_out_of_order_batch_is_refused(
        self, store: PostgresHistoryStore
    ) -> None:
        journal = Journal(SESSION)
        journal.user("one")
        journal.user("two")
        records = list(journal.records())

        lease = await store.acquire(SESSION)
        try:
            with pytest.raises(HistoryError, match="out of order"):
                await store.append(lease, list(reversed(records)))
        finally:
            await store.release(lease)


class TestAgent:
    async def test_two_turns_and_resume_keep_the_request_prefix(
        self,
        server: FakeServer,
        agents: StandAgents,
        store: PostgresHistoryStore,
        stores: Stores,
    ) -> None:
        served = Served(
            agents.agent(agents.profile(Protocol.OPENAI), Collected(), store=store)
        )
        session = await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(ScenarioName.THINKING_ANSWER.value))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()
        before = list(session.records())
        last_request = server.requests()[-1]["messages"]

        restored, report = await loaded(stores.new(), SESSION)
        assert report.stored_seq == before[-1].seq
        assert list(restored.records()) == before

        resumed = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI), Collected(), store=stores.new()
            )
        )
        session_again = await resumed.open(SESSION, resume=True)
        resumed.agent.queue.enqueue(prompt(ScenarioName.THINKING.value))
        await resumed.agent.settled()
        await resumed.close()

        records = list(session_again.records())
        assert records[: len(before)] == before
        assert kinds(records[len(before) :]) == [
            "queue-operation",
            "queue-operation",
            "user",
            "assistant",
            "assistant",
        ]
        after_request = server.requests()[-1]["messages"]
        assert isinstance(last_request, list)
        assert isinstance(after_request, list)
        assert after_request[: len(last_request)] == last_request

    async def test_every_cut_point_restores_the_same_as_jsonl(
        self,
        agents: StandAgents,
        store: PostgresHistoryStore,
        stores: Stores,
        tmp_path: Path,
    ) -> None:
        """Журнал JSONL двух ходов обрывается на каждой записи; те же записи
        в Postgres: загрузчик даёт одинаковые сохранённые записи и одинаковые
        виды починки."""
        served = Served(agents.agent(agents.profile(Protocol.OPENAI), Collected()))
        await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(ScenarioName.THINKING_ANSWER.value))
        await served.agent.settled()
        served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
        await served.agent.settled()
        await served.close()

        source = tmp_path / "history" / f"{SESSION.value}.jsonl"
        lines = source.read_text(encoding="utf-8").splitlines()
        codec = RecordCodec()
        for kept in range(1, len(lines) + 1):
            root = tmp_path / f"cut-{kept}"
            root.mkdir()
            (root / source.name).write_text(
                "\n".join(lines[:kept]) + "\n", encoding="utf-8"
            )
            cut_session = SessionId(value=f"cut-{kept}")
            records = [codec.parse(json.loads(line)) for line in lines[:kept]]
            lease = await store.acquire(cut_session)
            try:
                await store.append(lease, records)
            finally:
                await store.release(lease)

            from_jsonl, jsonl_report = await loaded(JsonlHistoryStore(root), SESSION)
            from_pg, pg_report = await loaded(stores.new(), cut_session)
            stored = jsonl_report.stored_seq
            assert pg_report.stored_seq == stored, kept
            assert (
                list(from_pg.records())[:stored] == list(from_jsonl.records())[:stored]
            )
            assert kinds(from_pg.records()[stored:]) == kinds(
                from_jsonl.records()[stored:]
            ), kept


class TestLargeJournal:
    @pytest.mark.integration
    async def test_resume_of_tens_of_megabytes(
        self, store: PostgresHistoryStore, stores: Stores
    ) -> None:
        """Журнал около 20 МБ: дозапись пачками по 2000 и возобновление;
        время печатается и сверяется с потолком."""
        journal = Journal(SESSION)
        text = "x" * 400
        for _ in range(25_000):
            journal.user(text)
            journal.assistant(text)

        records = list(journal.records())
        size = sum(len(RecordCodec().line(r)) for r in records[:1000]) * (
            len(records) // 1000
        )
        lease = await store.acquire(SESSION)
        try:
            for start in range(0, len(records), 2000):
                await store.append(lease, records[start : start + 2000])
        finally:
            await store.release(lease)

        started = time.monotonic()
        session, report = await loaded(stores.new(), SESSION)
        elapsed = time.monotonic() - started

        assert report.stored_seq == len(records)
        assert len(session.records()) == len(records)
        megabytes = size / 1_000_000
        print(
            f"\nresume of {megabytes:.1f} MB ({len(records)} records): {elapsed:.1f} s"
        )
        assert elapsed < 60.0


def call(spec: dict[str, object]) -> str:
    return f"{ScenarioName.CALL.value} {json.dumps(spec)}"


@pytest.fixture
async def metered_server() -> AsyncIterator[FakeServer]:
    """Стенд с учётом токенов по размеру запроса: порог сжатия достигается."""
    fake = FakeServer(estimate_usage=True)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


class TestStagesOnPostgres:
    """Проверки этапов 3 и 5 на этом хранилище: правило и режим живут после
    возобновления, сжатие пишет границу и пересказ, возобновление после сжатия
    даёт тот же запрос."""

    async def test_rule_and_mode_survive_resume(
        self, server: FakeServer, agents: StandAgents, stores: Stores
    ) -> None:
        tools = StandTools()
        sink = Collected()
        served = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI), sink, tools=tools, store=stores.new()
            )
        )
        await served.open(SESSION, resume=False)
        served.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.value})))
        question = await sink.next_question(0)
        rule = PermissionRule(tool="drop", behavior=PermissionBehavior.ALLOW)
        assert served.agent.answer(
            question.request_id, AllowAnswer(updated_permissions=[rule])
        )
        await served.agent.settled()
        served.agent.set_permission_mode(PermissionMode.ACCEPT_EDITS)
        await served.close()

        again = Collected()
        resumed = Served(
            agents.agent(
                agents.profile(Protocol.OPENAI), again, tools=tools, store=stores.new()
            )
        )
        await resumed.open(SESSION, resume=True)
        assert resumed.agent.permission_mode is PermissionMode.ACCEPT_EDITS
        resumed.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.value})))
        await resumed.agent.settled()
        resumed.agent.queue.enqueue(prompt(call({"name": StandToolName.NOTE.value})))
        await resumed.agent.settled()
        await resumed.close()

        assert not again.questions()
        assert [c.tool for c in tools.calls] == ["drop", "drop", "note"]

    async def test_compaction_and_resume_after_it(
        self,
        metered_server: FakeServer,
        tmp_path: Path,
        stores: Stores,
    ) -> None:
        agents = StandAgents(metered_server, tmp_path / "history")
        try:
            profile = agents.profile(Protocol.OPENAI, chars_per_token=4)
            served = Served(
                agents.agent(
                    profile, Collected(), tools=StandTools(), store=stores.new()
                )
            )
            session = await served.open(SESSION, resume=False)
            served.agent.queue.enqueue(
                prompt(
                    call(
                        {
                            "name": StandToolName.LARGE.value,
                            "arguments": {"size": 24_000},
                        }
                    )
                )
            )
            await served.agent.settled()
            served.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
            await served.agent.settled()
            await served.close()

            boundaries = [
                r for r in session.records() if isinstance(r, CompactBoundaryRecord)
            ]
            assert len(boundaries) == 1
            after = metered_server.requests()[-1]["messages"]

            resumed = Served(
                agents.agent(
                    profile, Collected(), tools=StandTools(), store=stores.new()
                )
            )
            session_again = await resumed.open(SESSION, resume=True)
            assert isinstance(session_again.conversation()[0], CompactBoundaryRecord)
            resumed.agent.queue.enqueue(prompt(ScenarioName.ANSWER.value))
            await resumed.agent.settled()
            await resumed.close()

            assert isinstance(after, list)
            prefix = metered_server.requests()[-1]["messages"]
            assert isinstance(prefix, list)
            assert prefix[: len(after)] == after
        finally:
            await agents.aclose()
