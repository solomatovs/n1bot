"""Память процесса `boba --stdio` на каждом запросе ACP при длинной истории.

Агент работает потоком: записи сессии читаются из Postgres порциями и
уходят клиенту по одной, а в памяти сессии остаётся только то, что нужно
модели, — разговор от последней полной границы сжатия. Поэтому пик памяти
процесса (VmHWM из /proc, а не ru_maxrss) на каждом запросе не растёт с
длиной истории: два процесса проходят один и тот же сценарий над сессией
короткой и сессией вдесятеро длиннее (с границами сжатия и крупными
записями), и прирост пика на каждом запросе у длинной не больше, чем у
короткой, с запасом на шум.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, TypeVar

import pytest
from acp.experimental.v2 import schema as acp_schema
from test_cli_acp import AcpClient, AcpProcess, FakeModel, StandRegistration, handshake
from test_cli_acp_postgres import PostgresStand

from boba.agent.history import SessionId
from boba.agent.records import (
    AssistantMessage,
    AssistantRecord,
    CompactBoundaryRecord,
    CompactMetadata,
    CompactTrigger,
    MessageOrigin,
    OriginKind,
    PreservedMessages,
    Record,
    ReplyStopReason,
    TextBlock,
    Usage,
    UserMessage,
    UserRecord,
)
from boba.db.postgres import AsyncPostgresPool
from boba.stand.ui.fake_llm import ScenarioName
from boba.transport.acp.protocol import (
    ClientConnection,
    MetaKey,
)

pytestmark = pytest.mark.anyio

T = TypeVar("T")

STAMP = "2026-10-23T10:00:00.000Z"
VERSION = "0.0.0-memory"


class Request(StrEnum):
    """Запросы сценария: по ним сверяется прирост пика."""

    INITIALIZE = "initialize"
    RESUME = "session/resume"
    SET_MODE = "session/set_config_option"
    PROMPT_QUEUED = "session/prompt (second one queued)"
    CANCEL = "session/cancel"
    LIST = "session/list"
    FORK = "session/fork"
    CLOSE = "session/close"
    DELETE = "session/delete"
    NEW = "session/new"


class ProcStatus:
    """Память процесса из /proc/<pid>/status: пик VmHWM в килобайтах."""

    FIELD: ClassVar[str] = "VmHWM:"

    def peak_kb(self, pid: int) -> int:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        for line in status.splitlines():
            if line.startswith(self.FIELD):
                return int(line.split()[1])

        msg = f"/proc/{pid}/status has no {self.FIELD} line"
        raise AssertionError(msg)


class LongHistory:
    """Сессия в Postgres: циклы из пар вопрос/ответ с крупным текстом, после
    каждого цикла — полная граница сжатия с пересказом и перенесённым
    хвостом. Пишется пачками, не собираясь в памяти теста целиком."""

    TEXT: ClassVar[int] = 4096
    CYCLE: ClassVar[int] = 100
    BATCH: ClassVar[int] = 500

    def __init__(self, session_id: SessionId, pairs: int) -> None:
        self._session = session_id
        self._pairs = pairs
        self._seq = 0
        self._parent: str | None = None
        self.records = 0

    async def write(self, stand: PostgresStand, pool: AsyncPostgresPool) -> None:
        store = stand.store(pool)
        lease = await store.acquire(self._session)
        try:
            batch: list[Record] = []
            for record in self._generated():
                batch.append(record)
                if len(batch) < self.BATCH:
                    continue

                await store.append(lease, batch)
                batch = []

            if batch:
                await store.append(lease, batch)
        finally:
            await store.release(lease)
            await store.close()

    def _generated(self) -> Iterator[Record]:
        recent: list[str] = []
        for number in range(self._pairs):
            question = self._user(f"question {number} " + "q" * self.TEXT, human=True)
            answer = self._assistant(f"answer {number} " + "a" * self.TEXT)
            recent = [question.uuid, answer.uuid]
            yield question
            yield answer
            if (number + 1) % self.CYCLE != 0:
                continue

            yield from self._compaction(recent)

    def _compaction(self, kept: list[str]) -> Iterator[Record]:
        summary_uuid = str(uuid.uuid4())
        boundary = CompactBoundaryRecord(
            uuid=str(uuid.uuid4()),
            parent_uuid=None,
            timestamp=STAMP,
            session_id=self._session.value,
            version=VERSION,
            seq=self._next(),
            logical_parent_uuid=kept[-1],
            compact_metadata=CompactMetadata(
                trigger=CompactTrigger.AUTO,
                pre_tokens=100_000,
                post_tokens=1_000,
                duration_ms=1,
                preserved_messages=PreservedMessages(
                    anchor_uuid=summary_uuid, uuids=kept, all_uuids=kept
                ),
            ),
        )
        self._parent = boundary.uuid
        yield boundary
        summary = UserRecord(
            uuid=summary_uuid,
            parent_uuid=self._parent,
            timestamp=STAMP,
            session_id=self._session.value,
            version=VERSION,
            seq=self._next(),
            message=UserMessage(content="summary of the conversation so far"),
            is_compact_summary=True,
        )
        self._parent = summary.uuid
        yield summary

    def _user(self, text: str, human: bool) -> UserRecord:
        origin = None
        if human:
            origin = MessageOrigin(kind=OriginKind.HUMAN)

        record = UserRecord(
            uuid=str(uuid.uuid4()),
            parent_uuid=self._parent,
            timestamp=STAMP,
            session_id=self._session.value,
            version=VERSION,
            seq=self._next(),
            message=UserMessage(content=text),
            origin=origin,
        )
        self._parent = record.uuid

        return record

    def _assistant(self, text: str) -> AssistantRecord:
        record = AssistantRecord(
            uuid=str(uuid.uuid4()),
            parent_uuid=self._parent,
            timestamp=STAMP,
            session_id=self._session.value,
            version=VERSION,
            seq=self._next(),
            message=AssistantMessage(
                id=f"msg_{uuid.uuid4().hex}",
                model="fake-model",
                content=[TextBlock(text=text)],
                stop_reason=ReplyStopReason.END_TURN,
                usage=Usage(input_tokens=10, output_tokens=5),
            ),
            request_id=f"req_{uuid.uuid4().hex}",
        )
        self._parent = record.uuid

        return record

    def _next(self) -> int:
        self._seq += 1
        self.records = self._seq

        return self._seq


@dataclass(frozen=True)
class Peaks:
    """Прирост пика памяти процесса на каждом запросе, в килобайтах."""

    records: int
    growth: dict[Request, int]


class Scenario:
    """Один процесс агента и все запросы сценария над длинной сессией с
    замером пика VmHWM до и после каждого запроса."""

    WINDOW: ClassVar[int] = 4_000_000
    """Окно контекста профиля: разговор от границы сжатия в него входит."""

    def __init__(
        self,
        root: Path,
        model: FakeModel,
        stand: PostgresStand,
    ) -> None:
        self._root = root
        self._process = AcpProcess(root, model, environment=stand.environment())
        self._stand = stand
        self._status = ProcStatus()
        self._growth: dict[Request, int] = {}

    async def run(self, session: SessionId, records: int) -> Peaks:
        client = AcpClient()
        registration = StandRegistration([StandRegistration.STAND], 0)
        document = self._stand.settings(self._process, None)
        document["models"]["fake"] = self._process.profile_document(
            "fake-model", context_window=self.WINDOW
        )
        conn = await self._process.start(client, registration, document)
        try:
            await self._measured(Request.INITIALIZE, handshake(conn))
            await self._steps(conn, client, session)
        finally:
            await self._process.close_stdin()
            await self._process.wait()

        return Peaks(records=records, growth=dict(self._growth))

    async def _steps(
        self, connection: ClientConnection, client: AcpClient, session: SessionId
    ) -> None:
        cwd = str(self._root)
        await self._measured(
            Request.RESUME,
            connection.resume_session(
                session_id=session.value,
                cwd=cwd,
                mcp_servers=[],
                replay_from=acp_schema.ReplayFromStartVariant(),
            ),
        )
        await self._measured(
            Request.SET_MODE,
            connection.set_config_option(
                config_id="mode", session_id=session.value, value="acceptEdits"
            ),
        )
        await self._measured(
            Request.PROMPT_QUEUED, self._two_prompts(connection, client, session)
        )
        await self._measured(
            Request.CANCEL, connection.cancel_session(session_id=session.value)
        )
        await self._measured(Request.LIST, connection.list_sessions())
        last = client.of(acp_schema.UserMessageUpdate)[-1].message_id
        meta: dict[str, Any] = {MetaKey.UP_TO_MESSAGE_ID.value: last}
        forked = await self._measured(
            Request.FORK,
            connection.fork_session(
                session_id=session.value, cwd=cwd, mcp_servers=[], **meta
            ),
        )
        await self._measured(
            Request.CLOSE, connection.close_session(session_id=session.value)
        )
        await self._measured(
            Request.DELETE, connection.delete_session(session_id=forked.session_id)
        )
        await self._measured(
            Request.NEW, connection.new_session(cwd=cwd, mcp_servers=[])
        )

    async def _two_prompts(
        self, connection: ClientConnection, client: AcpClient, session: SessionId
    ) -> None:
        text = [acp_schema.TextContentBlock(text=ScenarioName.ANSWER.value)]
        await connection.prompt(session_id=session.value, prompt=list(text))
        await connection.prompt(session_id=session.value, prompt=list(text))
        await client.idle(session.value)
        await client.idle(session.value)

    async def _measured(self, request: Request, work: Awaitable[T]) -> T:
        pid = self._process.process.pid
        before = self._status.peak_kb(pid)
        done = await work
        self._growth[request] = self._status.peak_kb(pid) - before

        return done


class TestMemoryDoesNotGrowWithHistory:
    """Пик памяти на каждом запросе ACP у сессии в 10 раз длиннее не выше,
    чем у короткой, с запасом на шум."""

    SHORT: ClassVar[int] = 600
    LONG: ClassVar[int] = 6000
    NOISE_KB: ClassVar[int] = 24 * 1024
    """Запас на шум аллокатора: длинная история весит десятки мегабайт, и
    накопление её в памяти этот запас перекрывает с избытком."""

    @pytest.mark.integration
    async def test_every_request_keeps_the_peak_flat(
        self,
        model: FakeModel,
        stand: PostgresStand,
        pool: AsyncPostgresPool,
        tmp_path: Path,
    ) -> None:
        peaks: list[Peaks] = []
        for pairs in (self.SHORT, self.LONG):
            session = SessionId(value=str(uuid.uuid4()))
            history = LongHistory(session, pairs)
            await history.write(stand, pool)
            root = tmp_path / f"pairs-{pairs}"
            root.mkdir()
            scenario = Scenario(root, model, stand)
            peaks.append(await scenario.run(session, history.records))

        short, long = peaks
        report: list[str] = []
        for request in Request:
            report.append(
                f"{request.value}: {short.growth[request]} KB at {short.records} "
                f"records, {long.growth[request]} KB at {long.records} records"
            )

        print("\n" + "\n".join(report))
        for request in Request:
            grown = long.growth[request] - short.growth[request]
            assert grown <= self.NOISE_KB, "\n".join(report)
