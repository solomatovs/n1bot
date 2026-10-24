"""Остановка агента: `boba --stdio` по концу ввода и по SIGTERM, `boba serve`
по SIGTERM — в простое и посреди хода, модулем venv и собранным бинарником
(маркер binary). Процесс выходит с кодом 0 без трассировки в журнале, каждая
открытая сессия закрыта в своём потоке, пул истории Postgres закрыт в цикле,
которому принадлежит, и в pg_stat_activity не остаётся соединений процесса."""

from __future__ import annotations

import asyncio
import signal
import sys
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, LiteralString

import pytest
from acp.experimental.v2 import schema as acp_schema
from acp.ws import create_websocket_stream
from test_cli_acp import AcpClient, AcpProcess, FakeModel, StandRegistration, handshake
from test_cli_acp_postgres import PostgresStand
from test_cli_binary import ReleaseBin, ReleaseBinary
from test_cli_serve import Connected, HistoryStand, Profile, StandTools

from boba.cli.serve import ServeHistory, ServeMark
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.runtime.config import ProcessConfig
from boba.stand.agent import FakeServer
from boba.stand.agent_service import (
    AgentServiceProcess,
    ServeAddress,
    ServeStandConfig,
    ServiceProfiles,
)
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandPorts

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

TOKEN_DELAY_SEC = 0.2
"""Пауза между кусками ответа модели: ход LONG длится секунды."""


class ShutdownLog(StrEnum):
    """Строки журнала агента, по которым видна остановка."""

    TRACEBACK = "Traceback"
    POOL_CLOSED = "AsyncPostgresPool closed"
    SESSION_CLOSED = "session {session_id}: closed, its thread is stopped"

    def of(self, session_id: str) -> str:
        return self.value.format(session_id=session_id)


class AgentCommand(StrEnum):
    """Чем запускается агент: модулем venv или бинарником выпуска."""

    MODULE = "module"
    BINARY = "binary"

    def argv(self, request: pytest.FixtureRequest) -> list[str]:
        if self is AgentCommand.BINARY:
            release = ReleaseBin(request.config.getoption("boba_bin", default=None))
            return [str(release.path(ReleaseBinary.BOBA))]

        return [sys.executable, "-m", AcpProcess.MODULE]


class Stopped:
    """Итог остановки процесса агента: код выхода и журнал целиком."""

    def __init__(self, code: int, log: str) -> None:
        self.code = code
        self.log = log

    def assert_clean(self, sessions: Sequence[str]) -> None:
        """Код 0, журнал без трассировки, пул истории закрыт, каждая сессия
        закрыта в своём потоке."""
        tail = self.log[-3000:]
        assert self.code == 0, tail
        assert ShutdownLog.TRACEBACK.value not in self.log, tail
        assert ShutdownLog.POOL_CLOSED.value in self.log, tail
        for session_id in sessions:
            assert ShutdownLog.SESSION_CLOSED.of(session_id) in self.log, tail


class PgBackends:
    """Соединения процесса агента в pg_stat_activity: подключение истории
    процесса подписано своим application_name."""

    WAIT_SEC: ClassVar[float] = 10.0
    POLL_SEC: ClassVar[float] = 0.1
    QUERY: ClassVar[LiteralString] = (
        "select count(*) from pg_stat_activity where application_name = %s"
    )

    def __init__(self, pool: AsyncPostgresPool, postgres: PostgresConfig) -> None:
        self._name = f"boba-shutdown-{uuid.uuid4().hex[:12]}"
        self.postgres = postgres.model_copy(update={"application_name": self._name})
        self._pool = pool

    async def count(self) -> int:
        async with self._pool.cursor() as cur:
            await cur.execute(self.QUERY, (self._name,))
            row = await cur.fetchone()

        assert row is not None
        return int(row[0])

    async def assert_gone(self) -> None:
        """Ни одного соединения процесса не осталось за WAIT_SEC."""
        deadline = time.monotonic() + self.WAIT_SEC
        while True:
            left = await self.count()
            if left == 0:
                return

            assert time.monotonic() < deadline, (
                f"pg_stat_activity still holds {left} connection(s) of "
                f"application_name {self._name} {self.WAIT_SEC}s after the exit"
            )
            await asyncio.sleep(self.POLL_SEC)


class FirstChunk:
    """Ход идёт: после первых seen уведомлений сессии пришёл кусок ответа."""

    WAIT_SEC: ClassVar[float] = 60.0
    POLL_SEC: ClassVar[float] = 0.05

    def __init__(self, updates: list[tuple[str, Any]], session_id: str) -> None:
        self._updates = updates
        self._session_id = session_id
        self._seen = len(updates)

    async def wait(self) -> None:
        deadline = time.monotonic() + self.WAIT_SEC
        while not self._arrived():
            assert time.monotonic() < deadline, "the turn sent no answer chunk"
            await asyncio.sleep(self.POLL_SEC)

    def _arrived(self) -> bool:
        for owner, update in self._updates[self._seen :]:
            if owner != self._session_id:
                continue

            if isinstance(update, acp_schema.AgentMessageChunk):
                return True

        return False


class InFlight:
    """Ход клиента, который шёл в момент сигнала: процесс вышел, ответа на
    вопрос клиент может и не получить — задача ожидания снимается."""

    def __init__(self, task: asyncio.Task[Any]) -> None:
        self._task = task

    async def drop(self) -> None:
        self._task.cancel()
        await asyncio.wait({self._task})


@pytest.fixture(
    params=[
        AgentCommand.MODULE,
        pytest.param(AgentCommand.BINARY, marks=pytest.mark.binary),
    ]
)
def command(request: pytest.FixtureRequest) -> list[str]:
    chosen = request.param
    assert isinstance(chosen, AgentCommand)

    return chosen.argv(request)


@pytest.fixture
def backends(pool: AsyncPostgresPool, postgres: PostgresConfig) -> PgBackends:
    return PgBackends(pool, postgres)


@pytest.fixture
async def slow_model() -> AsyncIterator[FakeModel]:
    fake = FakeModel(token_delay_sec=TOKEN_DELAY_SEC)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def serve_model() -> AsyncIterator[FakeServer]:
    fake = FakeServer(token_delay_sec=TOKEN_DELAY_SEC)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


class StdioRun:
    """`boba --stdio` с историей в Postgres стенда и двумя открытыми
    сессиями; в первой прошёл ход."""

    SESSIONS: ClassVar[int] = 2

    def __init__(self, boba: AcpProcess, backends: PgBackends) -> None:
        self.boba = boba
        self.client = AcpClient()
        self.sessions: list[str] = []
        self._backends = backends

    async def start(
        self, stand: PostgresStand, registration: StandRegistration, cwd: Path
    ) -> None:
        conn = await self.boba.start(
            self.client, registration, stand.settings(self.boba, None)
        )
        await handshake(conn)
        for _ in range(self.SESSIONS):
            opened = await conn.new_session(
                cwd=str(cwd), mcp_servers=[StandRegistration.STAND]
            )
            self.sessions.append(opened.session_id)

        await self.turn(self.sessions[0], ScenarioName.ANSWER)
        assert await self._backends.count() > 0

    async def turn(self, session_id: str, scenario: ScenarioName) -> None:
        prompt = [acp_schema.TextContentBlock(text=scenario.value)]
        await self.boba.turn(self.client, session_id, prompt)

    async def stopped(self) -> Stopped:
        code = await self.boba.wait()
        await self.boba.kill()

        return Stopped(code, "\n".join(self.boba.stderr))


class TestStdio:
    """`boba --stdio`: конец ввода и SIGTERM в простое и посреди хода."""

    @pytest.fixture
    async def run(  # noqa: PLR0913 — фикстуры стенда
        self,
        slow_model: FakeModel,
        command: list[str],
        registration: StandRegistration,
        backends: PgBackends,
        schema: str,
        tmp_path: Path,
    ) -> AsyncIterator[StdioRun]:
        stand = PostgresStand(backends.postgres, schema)
        boba = AcpProcess(tmp_path, slow_model, command, stand.environment())
        run = StdioRun(boba, backends)
        try:
            await run.start(stand, registration, tmp_path)
            yield run
        finally:
            await boba.kill()

    async def test_end_of_input(self, run: StdioRun, backends: PgBackends) -> None:
        await run.boba.close_stdin()

        stopped = await run.stopped()
        stopped.assert_clean(run.sessions)
        await backends.assert_gone()

    async def test_sigterm_in_idle(self, run: StdioRun, backends: PgBackends) -> None:
        run.boba.process.send_signal(signal.SIGTERM)

        stopped = await run.stopped()
        stopped.assert_clean(run.sessions)
        await backends.assert_gone()

    async def test_sigterm_in_a_turn(self, run: StdioRun, backends: PgBackends) -> None:
        running = run.sessions[1]
        chunk = FirstChunk(run.client.updates, running)
        turn = asyncio.create_task(run.turn(running, ScenarioName.LONG))
        await chunk.wait()
        run.boba.process.send_signal(signal.SIGTERM)

        stopped = await run.stopped()
        await InFlight(turn).drop()
        stopped.assert_clean(run.sessions)
        await backends.assert_gone()


class ServeRun:
    """`boba serve` отдельным процессом с историей в Postgres стенда:
    подключение по WebSocket, две открытые сессии, в первой прошёл ход."""

    SESSIONS: ClassVar[int] = 2
    LOGIN: ClassVar[str] = "alice"
    ROLES: ClassVar[str] = "dev"

    def __init__(self, service: AgentServiceProcess, backends: PgBackends) -> None:
        self.service = service
        self.sessions: list[str] = []
        self._backends = backends
        self._connected: Connected | None = None

    @property
    def connected(self) -> Connected:
        assert self._connected is not None
        return self._connected

    async def start(self, cwd: Path) -> None:
        await self.service.start()
        token = await self.service.token(self.LOGIN, self.ROLES)
        transport = await create_websocket_stream(
            self.service.ws_url(), headers={"Authorization": f"Bearer {token}"}
        )
        self._connected = Connected(transport, StandTools())
        await self.connected.initialize()
        await self.connected.conn.login(method_id=ServeMark.AUTH_METHOD)
        for _ in range(self.SESSIONS):
            opened = await self.connected.new_session(cwd)
            self.sessions.append(opened.session_id)

        await self.connected.turn(self.sessions[0], ScenarioName.ANSWER.value)
        assert await self._backends.count() > 0

    async def stopped(self) -> Stopped:
        code = await self.service.terminate()

        return Stopped(code, self.service.log())


class TestServe:
    """`boba serve`: SIGTERM в простое и посреди хода."""

    @pytest.fixture
    async def run(  # noqa: PLR0913 — фикстуры стенда
        self,
        serve_model: FakeServer,
        command: list[str],
        process_config: ProcessConfig,
        backends: PgBackends,
        schema: str,
        tmp_path: Path,
    ) -> AsyncIterator[ServeRun]:
        history = HistoryStand(backends.postgres, schema)
        profiles = ServiceProfiles(f"http://127.0.0.1:{serve_model.port}/v1").add(
            Profile.MAIN, [ServeRun.ROLES], Profile.MAIN_MODEL
        )
        port = StandPorts().free()
        config = ServeStandConfig(
            process_config.krb,
            ServeHistory(schema=history.schema, postgres=history.postgres),
            history.layer(ServeRun.SESSIONS),
            profiles,
            600.0,
            ServeAddress(listen=port, public=port),
        )
        service = AgentServiceProcess(command, tmp_path / "serve", config)
        run = ServeRun(service, backends)
        try:
            await run.start(tmp_path)
            yield run
        finally:
            await service.kill()

    async def test_sigterm_in_idle(self, run: ServeRun, backends: PgBackends) -> None:
        stopped = await run.stopped()

        stopped.assert_clean(run.sessions)
        await backends.assert_gone()

    async def test_sigterm_in_a_turn(self, run: ServeRun, backends: PgBackends) -> None:
        running = run.sessions[1]
        client = run.connected.client
        chunk = FirstChunk(client.updates, running)
        turn = asyncio.create_task(run.connected.turn(running, ScenarioName.LONG.value))
        await chunk.wait()

        stopped = await run.stopped()
        await InFlight(turn).drop()
        stopped.assert_clean(run.sessions)
        await backends.assert_gone()
