"""Хранилище истории в Postgres на настоящей базе стенда: контракт порта
(повтор пачки без дублей, каркас записи колонками и документ без него,
занятая сессия, откат пачки целиком, порядок чтения, список сессий
страницами, удаление под замком, копия до среза под новой сессией), агент
на стенде модели с этим хранилищем и возобновление, обрыв на каждой записи
даёт тот же восстановленный список, что JSONL; возобновление журнала в
десятки мегабайт — под меткой integration."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, LiteralString

import pytest
from omegaconf import DictConfig
from psycopg import AsyncConnection, sql
from psycopg.rows import tuple_row

from boba.agent.control import AllowAnswer
from boba.agent.history import (
    HistoryError,
    HistoryStore,
    RecordNotFoundError,
    SessionHeldError,
    SessionId,
    SessionNotFoundError,
    StoredLine,
)
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
from boba.history.postgres import PostgresHistoryStore, SessionLocks
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
from boba.stand.ui.stand import REPO_ROOT

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

    def user(
        self, text: str, tool_use_result: object = None, stamp: str = STAMP
    ) -> UserRecord:
        return self._session.add(
            UserRecord(
                uuid=self._ids.uuid(),
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=stamp,
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
        """Хранилище процесса без владельца: видит все сессии."""
        locks = SessionLocks(self._postgres)
        return PostgresHistoryStore(
            self._postgres, self._schema, None, locks, self._pool
        )

    def owned(self, owner: str) -> PostgresHistoryStore:
        """Хранилище процесса, запущенного службой от имени owner."""
        return PostgresHistoryStore(
            self._postgres,
            self._schema,
            owner,
            SessionLocks(self._postgres),
            self._pool,
        )


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
            with pytest.raises(SessionHeldError, match="already in use"):
                await other.acquire(SESSION)
        finally:
            await store.release(lease)

        again = await other.acquire(SESSION)
        await other.release(again)

    async def test_list_pages_by_session_id_with_the_last_record_time(
        self, store: PostgresHistoryStore
    ) -> None:
        for name, stamps in (
            ("list-b", ["2026-10-10T10:00:00.000Z"]),
            ("list-a", ["2026-10-10T09:00:00.000Z", "2026-10-10T11:00:00.000Z"]),
            ("list-c", ["2026-10-09T10:00:00.000Z"]),
        ):
            session = SessionId(value=name)
            journal = Journal(session)
            for stamp in stamps:
                journal.user(f"at {stamp}", stamp=stamp)

            lease = await store.acquire(session)
            try:
                await store.append(lease, list(journal.records()))
            finally:
                await store.release(lease)

        first = await store.list(None, 2)
        second = await store.list(first.next_cursor, 2)

        assert [s.session.value for s in first.sessions] == ["list-a", "list-b"]
        assert first.next_cursor == "list-b"
        assert first.sessions[0].updated_at == "2026-10-10T11:00:00.000Z"
        assert [s.session.value for s in second.sessions] == ["list-c"]
        assert second.next_cursor is None

    async def test_delete_removes_records_unless_the_session_is_held(
        self, store: PostgresHistoryStore, stores: Stores
    ) -> None:
        journal = Journal(SESSION)
        journal.user("one")
        lease = await store.acquire(SESSION)
        try:
            await store.append(lease, list(journal.records()))
            with pytest.raises(SessionHeldError, match="cannot be deleted"):
                await stores.new().delete(SESSION)
        finally:
            await store.release(lease)

        assert len(await read_all(store, SESSION)) == 1

        await stores.new().delete(SESSION)
        await stores.new().delete(SESSION)
        await store.delete(SessionId(value="never-existed"))

        assert await read_all(store, SESSION) == []
        assert (await store.list(None, 10)).sessions == []

    async def test_copy_branches_before_the_cut_and_the_branch_loads(
        self, store: PostgresHistoryStore, stores: Stores
    ) -> None:
        journal = Journal(SESSION)
        first = journal.user("one")
        reply = journal.assistant("two")
        second = journal.user("three")
        journal.assistant("four")
        records = list(journal.records())
        lease = await store.acquire(SESSION)
        try:
            await store.append(lease, records)
        finally:
            await store.release(lease)
        source_before = await read_all(store, SESSION)
        branch = SessionId(value="history-pg-branch")

        copied = await store.copy(SESSION, branch, second.uuid)

        assert copied == 2
        documents = await read_all(store, branch)
        assert [d["seq"] for d in documents] == [1, 2]
        assert [d["uuid"] for d in documents] == [first.uuid, reply.uuid]
        assert documents == [
            {**d, "sessionId": branch.value} for d in source_before[:2]
        ]
        assert await read_all(store, SESSION) == source_before

        session, report = await loaded(stores.new(), branch)
        assert report.repaired == []
        assert report.stored_seq == 2
        expected = [
            r.model_copy(update={"session_id": branch.value}) for r in records[:2]
        ]
        assert list(session.records()) == expected

        held = await stores.new().acquire(SESSION)
        try:
            whole = await store.copy(SESSION, SessionId(value="history-pg-whole"), None)
        finally:
            await stores.new().release(held)

        assert whole == 4
        assert len(await read_all(store, SessionId(value="history-pg-whole"))) == 4
        assert (
            await store.copy(SESSION, SessionId(value="history-pg-empty"), first.uuid)
            == 0
        )
        assert await read_all(store, SessionId(value="history-pg-empty")) == []

        with pytest.raises(RecordNotFoundError, match="no record with that uuid"):
            await store.copy(SESSION, SessionId(value="history-pg-x"), "nope")

        with pytest.raises(SessionNotFoundError, match="nothing to branch"):
            await store.copy(
                SessionId(value="never"), SessionId(value="history-pg-y"), None
            )

        with pytest.raises(HistoryError, match="already has records"):
            await store.copy(SESSION, branch, None)

        assert len(await read_all(store, branch)) == 2
        listed = [s.session.value for s in (await store.list(None, 10)).sessions]
        assert listed == ["history-pg-1", "history-pg-branch", "history-pg-whole"]

    async def test_rows_keep_the_frame_in_columns(
        self, store: PostgresHistoryStore, pool: AsyncPostgresPool, schema: str
    ) -> None:
        """Сессия, seq, вид и время лежат колонками, в record их нет; строку
        сессии заводит первая пачка со временем первой записи; запись чужой
        сессии не принимается."""
        journal = Journal(SESSION)
        journal.user("one", stamp="2026-10-10T09:00:00.250Z")
        journal.assistant("two")
        foreign = journal.user("three").model_copy(update={"session_id": "stranger"})
        records = list(journal.records())

        lease = await store.acquire(SESSION)
        try:
            await store.append(lease, records[:2])
            with pytest.raises(HistoryError, match="carries sessionId 'stranger'"):
                await store.append(lease, [foreign])
        finally:
            await store.release(lease)

        async with pool.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute(
                sql.SQL("select id, created_at from {}.session").format(
                    sql.Identifier(schema)
                )
            )
            sessions = await cur.fetchall()
            await cur.execute(
                sql.SQL(
                    "select session_id, seq, type, recorded_at, record "
                    "from {}.session_records order by seq"
                ).format(sql.Identifier(schema))
            )
            rows = await cur.fetchall()

        first = datetime(2026, 10, 10, 9, 0, 0, 250_000, tzinfo=UTC)
        assert sessions == [(SESSION.value, first)]
        assert [row[:4] for row in rows] == [
            (SESSION.value, 1, "user", first),
            (SESSION.value, 2, "assistant", datetime(2026, 10, 10, 12, tzinfo=UTC)),
        ]
        for row in rows:
            assert not {"type", "seq", "sessionId", "timestamp"} & set(row[4])

        documents = await read_all(store, SESSION)
        assert documents == [json.loads(RecordCodec().line(r)) for r in records[:2]]
        assert documents[0]["timestamp"] == "2026-10-10T09:00:00.250Z"

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


async def written(store: HistoryStore, session_id: SessionId, text: str) -> None:
    """Сессия с одной записью вопроса, заведённая хранилищем store."""
    journal = Journal(session_id)
    journal.user(text)
    lease = await store.acquire(session_id)
    try:
        await store.append(lease, list(journal.records()))
    finally:
        await store.release(lease)


async def listed(store: HistoryStore) -> list[str]:
    page = await store.list(None, 10)

    return [s.session.value for s in page.sessions]


class TestOwners:
    """Хранилище с владельцем заводит сессии на него и видит только их; чужая
    сессия и сессия без владельца для него не существуют. Хранилище без
    владельца видит все."""

    ALICE = SessionId(value="owner-alice")
    BOB = SessionId(value="owner-bob")
    NOBODY = SessionId(value="owner-nobody")

    async def test_each_owner_sees_only_own_sessions(self, stores: Stores) -> None:
        alice = stores.owned("alice")
        bob = stores.owned("bob")
        nobody = stores.new()
        await written(alice, self.ALICE, "of alice")
        await written(bob, self.BOB, "of bob")
        await written(nobody, self.NOBODY, "of nobody")

        assert await listed(alice) == [self.ALICE.value]
        assert await listed(bob) == [self.BOB.value]
        assert await listed(nobody) == [
            self.ALICE.value,
            self.BOB.value,
            self.NOBODY.value,
        ]
        assert await read_all(alice, self.BOB) == []
        assert await read_all(alice, self.NOBODY) == []
        assert len(await read_all(nobody, self.ALICE)) == 1

        _, report = await loaded(alice, self.BOB)
        assert report.stored_seq == 0

    async def test_foreign_session_is_not_found_for_every_operation(
        self, stores: Stores
    ) -> None:
        alice = stores.owned("alice")
        bob = stores.owned("bob")
        await written(alice, self.ALICE, "of alice")
        await written(stores.new(), self.NOBODY, "of nobody")

        for foreign in (self.ALICE, self.NOBODY):
            with pytest.raises(SessionNotFoundError, match="among the sessions of"):
                await bob.acquire(foreign)

            with pytest.raises(SessionNotFoundError, match="nothing to branch"):
                await bob.copy(foreign, SessionId(value="owner-stolen"), None)

            await bob.delete(foreign)

        assert await listed(stores.new()) == [self.ALICE.value, self.NOBODY.value]
        assert await listed(bob) == []

    async def test_copy_belongs_to_the_owner_of_the_store(self, stores: Stores) -> None:
        alice = stores.owned("alice")
        branch = SessionId(value="owner-alice-branch")
        await written(alice, self.ALICE, "of alice")

        assert await alice.copy(self.ALICE, branch, None) == 1

        assert await listed(alice) == [self.ALICE.value, branch.value]
        bob = stores.owned("bob")
        assert await listed(bob) == []
        await written(bob, self.BOB, "of bob")
        with pytest.raises(HistoryError, match="already has records"):
            await bob.copy(self.BOB, branch, None)

        await alice.delete(branch)
        assert await listed(alice) == [self.ALICE.value]


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
            moved = {"session_id": cut_session.value}
            records = [
                codec.parse(json.loads(line)).model_copy(update=moved)
                for line in lines[:kept]
            ]
            lease = await store.acquire(cut_session)
            try:
                await store.append(lease, records)
            finally:
                await store.release(lease)

            from_jsonl, jsonl_report = await loaded(JsonlHistoryStore(root), SESSION)
            from_pg, pg_report = await loaded(stores.new(), cut_session)
            stored = jsonl_report.stored_seq
            assert pg_report.stored_seq == stored, kept
            expected = [
                r.model_copy(update=moved) for r in from_jsonl.records()[:stored]
            ]
            assert list(from_pg.records())[:stored] == expected
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
        served.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.full})))
        question = await sink.next_question(0)
        rule = PermissionRule(
            tool="mcp__stand__drop", behavior=PermissionBehavior.ALLOW
        )
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
        resumed.agent.queue.enqueue(prompt(call({"name": StandToolName.DROP.full})))
        await resumed.agent.settled()
        resumed.agent.queue.enqueue(prompt(call({"name": StandToolName.NOTE.full})))
        await resumed.agent.settled()
        await resumed.close()

        assert not again.questions()
        assert [c.name for c in tools.calls] == [
            "mcp__stand__drop",
            "mcp__stand__drop",
            "mcp__stand__note",
        ]

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
                            "name": StandToolName.LARGE.full,
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


class TestCutoverTable:
    """Перенос чата на агента (build/scripts/migrations/2026-10-16-chat-agent-cutover)
    создаёт таблицы истории своим SQL до первого обращения агента: это
    раскладка выпуска 2026-10-16, и хранилище обязано довести её на месте до
    той, что создаёт само."""

    SCRIPT: ClassVar[Path] = (
        REPO_ROOT
        / "build/scripts/migrations/2026-10-16-chat-agent-cutover/history_tables.sql"
    )
    COLUMNS: ClassVar[LiteralString] = (
        "select table_name, column_name, data_type, is_nullable, column_default "
        "from information_schema.columns "
        "where table_schema = %s and table_name in ('session', 'session_records') "
        "order by table_name, column_name"
    )
    CONSTRAINTS: ClassVar[LiteralString] = (
        "select t.relname, k.contype, k.confdeltype, coalesce(r.relname, ''), "
        "coalesce(r.relnamespace = t.relnamespace, true), "
        "array("
        "select a.attname from unnest(k.conkey) with ordinality as c(attnum, n) "
        "join pg_attribute a on a.attrelid = k.conrelid and a.attnum = c.attnum "
        "order by c.n"
        ") as columns "
        "from pg_constraint k "
        "join pg_class t on t.oid = k.conrelid "
        "join pg_namespace s on s.oid = t.relnamespace "
        "left join pg_class r on r.oid = k.confrelid "
        "where s.nspname = %s and t.relname in ('session', 'session_records') "
        "order by t.relname, k.contype, columns"
    )

    LEGACY_ROWS: ClassVar[LiteralString] = (
        "insert into session (id, created_at) "
        "values ('legacy-1', '2026-10-10T09:00:00Z'); "
        "insert into session_records (session_id, seq, type, recorded_at, record) "
        "values ('legacy-1', 1, 'user', '2026-10-10T09:00:00Z', '{}')"
    )

    async def test_script_tables_migrate_in_place_to_the_store(
        self,
        store: PostgresHistoryStore,
        schema: str,
        pool: AsyncPostgresPool,
        postgres: PostgresConfig,
    ) -> None:
        """Таблицы скрипта переноса — раскладка прежнего выпуска без владельца:
        хранилище доводит их на месте до своей, строки остаются, их владелец
        неизвестен, и сессия видна только процессу без владельца."""
        cutover = f"{schema}_cutover"
        await store.list(None, 1)
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("create schema {}").format(sql.Identifier(cutover))
            )
            try:
                async with conn.transaction():
                    await conn.execute(
                        sql.SQL("set local search_path to {}").format(
                            sql.Identifier(cutover)
                        )
                    )
                    await conn.execute(self.SCRIPT.read_bytes())
                    await conn.execute(self.LEGACY_ROWS)

                legacy = await self._layout(conn, cutover)
                await self.prepared(postgres, cutover, pool)
                expected = await self._layout(conn, schema)
                built = await self._layout(conn, cutover)
                unowned = await listed(
                    PostgresHistoryStore(
                        postgres, cutover, None, SessionLocks(postgres), pool
                    )
                )
                owned = await listed(
                    PostgresHistoryStore(
                        postgres, cutover, "alice", SessionLocks(postgres), pool
                    )
                )
            finally:
                await conn.execute(
                    sql.SQL("drop schema {} cascade").format(sql.Identifier(cutover))
                )

        assert built == expected
        assert len(legacy[0]) == 7
        assert len(expected[0]) == 8
        assert len(expected[1]) >= 3
        assert unowned == ["legacy-1"]
        assert owned == []

    async def prepared(
        self, postgres: PostgresConfig, cutover: str, pool: AsyncPostgresPool
    ) -> None:
        """Первое обращение хранилища к схеме переноса применяет файлы схемы."""
        await PostgresHistoryStore(
            postgres, cutover, None, SessionLocks(postgres), pool
        ).list(None, 1)

    async def _layout(
        self, conn: AsyncConnection[Any], schema: str
    ) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
        async with conn.cursor(row_factory=tuple_row) as cur:
            await cur.execute(self.COLUMNS, (schema,))
            columns = await cur.fetchall()
            await cur.execute(self.CONSTRAINTS, (schema,))
            constraints = await cur.fetchall()

        return columns, constraints
