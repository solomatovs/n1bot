"""Стенд chainlit ведёт разговор через `boba acp`: приложение и его сервис
boba-mcp отдельными процессами, фейковая модель, вкладка — headless-клиент
socket.io тем же протоколом, что и браузер. Проверяется ход с потоком токенов
в ленту, остановка кнопкой Stop, sessionId агента в threads.meta, процесс
агента без секретов в argv и окружении и его уход вместе с вкладкой; возврат к
треду и второй инстанс (`session/load`) ждут W3."""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest
import socketio
from acp_stand import AgentHistoryStand, ProcFiles
from psycopg import sql

from boba.chainlit.acp.settings import WorkdirFile
from boba.chainlit.domain.fields import ThreadMetaField
from boba.db.postgres import AsyncPostgresPool
from boba.runtime.config import DataLayerConfig
from boba.stand.database import TestDatabase
from boba.stand.names import StandNames
from boba.stand.site import Stand
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import ScenarioName, serve
from boba.stand.ui.stand import (
    StandApp,
    StandConfig,
    StandProcess,
    StandUrl,
    free_port,
)

pytestmark = pytest.mark.anyio

DB_NAME = "boba_acp_chat"
BOOT_TIMEOUT_SEC = 180.0
TURN_TIMEOUT_SEC = 120.0
TOKEN_DELAY_SEC = 0.03
PROFILE = "general"
W3_REASON = "session/load and the observer process arrive with W3"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Стенд живёт отдельным процессом: сессия chainlit в тесте не нужна."""


@pytest.fixture(scope="module")
def stand_database() -> str:
    return StandDatabase(StandApp.CHAINLIT, StandNames().of(DB_NAME)).prepare()


@pytest.fixture(scope="module")
def llm_port() -> int:
    return free_port()


@pytest.fixture(scope="module")
def fake_llm(llm_port: int) -> Iterator[None]:
    process = multiprocessing.Process(
        target=serve, args=(StandUrl.HOST.value, llm_port, TOKEN_DELAY_SEC), daemon=True
    )
    process.start()
    try:
        _await_llm(llm_port)
        yield
    finally:
        process.terminate()
        process.join(timeout=10)


def _await_llm(port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if (
                httpx.get(StandUrl.of(port, "/v1/models"), timeout=1.0).status_code
                < 500
            ):
                return
        except httpx.HTTPError:
            time.sleep(0.2)

    msg = f"fake llm did not answer GET {StandUrl.of(port, '/v1/models')} in 30s"
    raise RuntimeError(msg)


@pytest.fixture(scope="module")
def app(
    tmp_path_factory: pytest.TempPathFactory,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    stand: Stand,
) -> Iterator[StandProcess]:
    workdir = tmp_path_factory.mktemp("acp-chat-stand")
    config = StandConfig(
        workdir=workdir,
        app=StandApp.CHAINLIT,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        agent_history=AgentHistoryStand(stand).document(),
    )
    process = StandProcess(config=config, log_path=workdir / "app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def cookie(app: StandProcess) -> str:
    credential = app.config.credential()
    response = httpx.post(
        f"{app.config.base_url}/login",
        data={"username": credential.login, "password": credential.password},
        timeout=30.0,
    )
    assert response.status_code == 200, response.text[:200]
    parts: list[str] = []
    for name, value in response.cookies.items():
        parts.append(f"{name}={value}")

    assert parts, "login returned no cookies"

    return "; ".join(parts)


class ChatSocket:
    """Вкладка чата без браузера: socket.io chainlit с cookie входа; копит
    события ленты и ждёт конца задачи."""

    SOCKET_PATH: ClassVar[str] = "/ws/socket.io"
    CLIENT_TYPE: ClassVar[str] = "webapp"

    def __init__(self, app: StandProcess, cookie: str, thread_id: str) -> None:
        self._app = app
        self._cookie = cookie
        self.thread_id = thread_id
        self.session_id = uuid4().hex
        self.events: list[tuple[str, Any]] = []
        self._task_ends = asyncio.Queue[None]()
        self._sio = socketio.AsyncClient(reconnection=False)
        self._sio.on("*", self._on_any)

    async def open(self) -> None:
        base = StandUrl.of(self._app.config.app_port)
        await self._sio.connect(
            base,
            socketio_path=f"{self._app.config.url_prefix}{self.SOCKET_PATH}",
            headers={"Cookie": self._cookie},
            auth={
                "sessionId": self.session_id,
                "clientType": self.CLIENT_TYPE,
                "threadId": self.thread_id,
                "chatProfile": PROFILE,
                "userEnv": "{}",
            },
            transports=["websocket"],
            wait_timeout=30,
        )
        await self._sio.emit("connection_successful")
        # on_chat_start выполнен: обёртка chainlit шлёт task_end на этот запрос
        await self.wait_task_end()

    async def send(self, text: str) -> None:
        message = {
            "id": str(uuid4()),
            "name": "user",
            "type": "user_message",
            "output": text,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "metadata": {},
        }
        await self._sio.emit("client_message", {"message": message})

    async def stop(self) -> None:
        await self._sio.emit("stop")

    async def wait_task_end(self, timeout_sec: float = TURN_TIMEOUT_SEC) -> None:
        await asyncio.wait_for(self._task_ends.get(), timeout_sec)

    async def wait_event(self, name: str, timeout_sec: float = TURN_TIMEOUT_SEC) -> Any:
        deadline = time.monotonic() + timeout_sec
        seen = 0
        while time.monotonic() < deadline:
            for event, payload in self.events[seen:]:
                seen += 1
                if event == name:
                    return payload

            await asyncio.sleep(0.05)

        msg = f"event {name!r} did not arrive in {timeout_sec}s: {self.names()}"
        raise AssertionError(msg)

    async def close(self) -> None:
        await self._sio.disconnect()

    def names(self) -> list[str]:
        return [event for event, _ in self.events]

    def tokens(self) -> str:
        found: list[str] = []
        for event, payload in self.events:
            if event == "stream_token":
                found.append(str(payload.get("token", "")))

        return "".join(found)

    def messages(self, kind: str = "assistant_message") -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for event, payload in self.events:
            if event not in ("new_message", "update_message", "stream_start"):
                continue

            if payload.get("type") == kind:
                found.append(payload)

        return found

    def errors(self) -> list[str]:
        found: list[str] = []
        for payload in self.messages("assistant_message"):
            if payload.get("isError"):
                found.append(str(payload.get("output")))

        return found

    async def _on_any(self, event: str, *args: Any) -> None:
        payload: Any = None
        if args:
            payload = args[0]

        self.events.append((event, payload))
        if event == "task_end":
            self._task_ends.put_nowait(None)


@pytest.fixture
async def tab(app: StandProcess, cookie: str) -> AsyncIterator[ChatSocket]:
    socket = ChatSocket(app, cookie, str(uuid4()))
    await socket.open()
    try:
        yield socket
    finally:
        await socket.close()


class AgentProcesses:
    """Процессы `boba acp`, запущенные стендом: по каталогу настроек в argv."""

    def __init__(self, app: StandProcess) -> None:
        self._agents_dir = app.config.workdir / "agents"

    def pids(self) -> list[int]:
        found: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue

            try:
                argv = ProcFiles(int(entry.name)).cmdline()
            except OSError:
                continue

            if any(part.startswith(str(self._agents_dir)) for part in argv):
                found.append(int(entry.name))

        return found

    def settings_dirs(self) -> list[Path]:
        if not self._agents_dir.is_dir():
            return []

        return sorted(self._agents_dir.iterdir())


class ChatRows:
    """Строки базы стенда: metadata треда."""

    def __init__(self, stand_data_layer: DataLayerConfig, database: str) -> None:
        self._postgres = TestDatabase.config_of(stand_data_layer.postgres, database)
        self._schema = stand_data_layer.db_schema

    async def thread_meta(self, thread_id: str) -> dict[str, Any] | None:
        pool = AsyncPostgresPool(self._postgres)
        await pool.open()
        try:
            async with pool.connection() as conn:
                cursor = await conn.execute(
                    sql.SQL("select meta from {}.threads where id = %s").format(
                        sql.Identifier(self._schema)
                    ),
                    (thread_id,),
                )
                row = await cursor.fetchone()
        finally:
            await pool.close()

        if row is None:
            return None

        return row[0]

    async def chat_settings_rows(self, thread_id: str) -> int:
        pool = AsyncPostgresPool(self._postgres)
        await pool.open()
        try:
            async with pool.connection() as conn:
                cursor = await conn.execute(
                    sql.SQL(
                        "select count(*) from {}.agent_chat_settings "
                        "where thread_id = %s"
                    ).format(sql.Identifier(self._schema)),
                    (thread_id,),
                )
                row = await cursor.fetchone()
        finally:
            await pool.close()

        assert row is not None

        return int(row[0])


@pytest.fixture
def rows(stand_data_layer: DataLayerConfig, stand_database: str) -> ChatRows:
    return ChatRows(stand_data_layer, stand_database)


class TestConversation:
    """Вкладка стенда ведёт разговор через процесс агента."""

    async def test_turn_streams_the_answer_into_the_feed(
        self, app: StandProcess, tab: ChatSocket, rows: ChatRows, stand: Stand
    ) -> None:
        agents = AgentProcesses(app)
        pids = agents.pids()
        assert len(pids) == 1, f"one agent per tab expected, got {pids}: {app.tail(60)}"

        files = ProcFiles(pids[0])
        joined = " ".join(files.cmdline())
        assert "--settings" in joined
        assert stand.pg_probe_password.get_secret_value() not in joined
        environ = files.environ()
        assert "BOBA_CONFIG_PATH" not in environ
        assert stand.pg_probe_password.get_secret_value() not in " ".join(
            environ.values()
        )
        assert "PGPASSWORD" not in environ

        since = app.log_lines()
        await tab.send(f"hello {ScenarioName.THINKING_ANSWER.value}")
        await tab.wait_task_end()

        assert not tab.errors(), (tab.errors(), app.tail(80))
        assert "answer the question" in tab.tokens(), tab.names()
        answers = tab.messages()
        assert answers, tab.names()
        assert any("answer the question" in str(m.get("output")) for m in answers)

        meta = await rows.thread_meta(tab.thread_id)
        assert meta is not None
        assert meta.get(ThreadMetaField.AGENT_SESSION), meta
        assert await rows.chat_settings_rows(tab.thread_id) == 1

        complaints = app.complaints(since)
        assert not complaints, complaints

    async def test_stop_cancels_the_turn(
        self, app: StandProcess, tab: ChatSocket
    ) -> None:
        await tab.send(f"go {ScenarioName.LONG.value}")
        await tab.wait_event("stream_token")
        await tab.stop()
        await tab.wait_task_end()

        assert not tab.errors(), (tab.errors(), app.tail(80))
        notes = [
            m for m in tab.messages() if "stopped by the user" in str(m.get("output"))
        ]
        assert notes, tab.names()

    async def test_closing_the_tab_ends_the_agent_and_removes_its_settings(
        self, app: StandProcess, cookie: str
    ) -> None:
        agents = AgentProcesses(app)
        before = set(agents.pids())
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        started = set(agents.pids()) - before
        assert len(started) == 1, (started, app.tail(60))
        pid = next(iter(started))
        settings_dirs = [
            path
            for path in agents.settings_dirs()
            if (path / WorkdirFile.SETTINGS.value).is_file()
        ]
        assert settings_dirs

        await tab.close()
        # сессия chainlit удаляется позже обрыва сокета: ждём ухода процесса
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and pid in agents.pids():
            await asyncio.sleep(0.5)

        assert pid not in agents.pids(), app.tail(60)
        left = [
            path for path in agents.settings_dirs() if path.name.startswith("thread-")
        ]
        assert all((path / WorkdirFile.SETTINGS.value).is_file() for path in left)
        assert not any(str(pid) in str(path) for path in left)


class TestPendingW3:
    """Возврат к треду и второй инстанс: история и живой ход через `session/load`."""

    @pytest.mark.xfail(strict=True, reason=W3_REASON)
    async def test_resumed_tab_shows_the_history(
        self, app: StandProcess, cookie: str
    ) -> None:
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        await tab.send(f"remember {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        await tab.close()

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open()
        try:
            assert not again.errors(), again.errors()
            assert again.messages("user_message"), again.names()
        finally:
            await again.close()

    @pytest.mark.xfail(strict=True, reason=W3_REASON)
    async def test_second_instance_follows_a_live_turn(  # noqa: PLR0913 — фикстуры
        self,
        app: StandProcess,
        cookie: str,
        llm_port: int,
        stand_database: str,
        stand: Stand,
        tmp_path: Path,
    ) -> None:
        first = ChatSocket(app, cookie, str(uuid4()))
        await first.open()
        await first.send(f"start {ScenarioName.ANSWER.value}")
        await first.wait_task_end()

        config = StandConfig(
            workdir=tmp_path / "second",
            app=StandApp.CHAINLIT,
            app_port=free_port(),
            llm_port=llm_port,
            db_name=stand_database,
            agent_history=AgentHistoryStand(stand).document(),
        )
        second = StandProcess(config=config, log_path=tmp_path / "second.log")
        second.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
        try:
            observer = ChatSocket(second, cookie, first.thread_id)
            await observer.open()
            try:
                assert not observer.errors(), observer.errors()
                assert observer.messages("user_message"), observer.names()
            finally:
                await observer.close()
        finally:
            second.stop()
            await first.close()
