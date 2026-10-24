"""Стенд aid ведёт разговор через `boba --stdio`: приложение и его сервис
boba-mcp отдельными процессами, фейковая модель, вкладка — headless-клиент
socket.io тем же протоколом, что и браузер. Проверяется ход с потоком токенов
в ленту, остановка кнопкой Stop, sessionId агента в threads.meta, процесс
агента без секретов в argv и окружении и его уход вместе с вкладкой; возврат к
треду воспроизводит историю через `session/resume`, второй инстанс видит её
и ведёт следующий ход своим процессом, шаг вопроса получает
messageId агента, правка вопроса по нему ветвит сессию, а у чужого агента
без среза ветки отказана и ничего не меняет; удаление треда удаляет сессию
агента и файлы треда; элементы хода (вложение, файл итога, ссылка панели)
после перезагрузки те же, что вживую; второй инстанс получает токены чужого хода
по шине Postgres, оценки возвращаются к шагам; панели настроек у вкладки нет, а
агент работает по профилю в режиме bypassPermissions. История агента — Postgres
стенда билетом Kerberos, как у самого чата."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest
import socketio
from acp_stand import ProcFiles
from psycopg import sql

from boba.aid.agent.settings import WorkdirFile
from boba.aid.chat.ui_text import UiText
from boba.aid.domain.fields import ThreadMetaField
from boba.aid.rendering.chat_view import StepMeta, StepText
from boba.aid.rendering.renderer import SignalType
from boba.canvas.canvas import CanvasAction, CanvasElement, CanvasKind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.runtime.config import RuntimeConfig
from boba.stand.database import TestDatabase
from boba.stand.names import StandNames
from boba.stand.site import Stand
from boba.stand.ui.database import StandDatabase
from boba.stand.ui.fake_llm import (
    FailureName,
    Reply,
    ReplyBookClient,
    ReplyTurn,
    ScenarioName,
    serve,
)
from boba.stand.ui.stand import (
    StandApp,
    StandConfig,
    StandPorts,
    StandProcess,
    StandToolNames,
    StandUrl,
)
from boba.toolkit.result import ContentMime

pytestmark = pytest.mark.anyio

DB_NAME = "boba_acp_chat"
BOOT_TIMEOUT_SEC = 180.0
TURN_TIMEOUT_SEC = 120.0
TOKEN_DELAY_SEC = 0.03
PROFILE = "general"
GENERATION = "acp-chat-stand"
MESSAGING = "postgres"
"""Шина в базе стенда: второй инстанс видит ход первого."""
PULSE = "\u200b"
FOREIGN_AGENT = Path(__file__).with_name("foreign_agent.py")
"""Чужой агент ACP v2 без среза ветки по messageId вопроса."""
STEP_TYPES = ("user_message", "run", "llm", "tool", "assistant_message")


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Стенд живёт отдельным процессом: сессия chainlit в тесте не нужна."""


@pytest.fixture(scope="module")
def stand_database() -> str:
    return StandDatabase(StandApp.AID, StandNames().of_tree(DB_NAME)).prepare()


@pytest.fixture(scope="module")
def llm_port() -> int:
    return StandPorts().free()


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
) -> Iterator[StandProcess]:
    workdir = tmp_path_factory.mktemp("acp-chat-stand")
    config = StandConfig(
        workdir=workdir,
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        generation=GENERATION,
        messaging=MESSAGING,
    )
    process = StandProcess(config=config, log_path=workdir / "app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def second(
    tmp_path_factory: pytest.TempPathFactory,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    app: StandProcess,
) -> Iterator[StandProcess]:
    """Второй инстанс aid над той же базой: наблюдатель чужих ходов."""
    workdir = tmp_path_factory.mktemp("acp-chat-second")
    config = StandConfig(
        workdir=workdir,
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        generation=GENERATION,
        messaging=MESSAGING,
    )
    process = StandProcess(config=config, log_path=workdir / "second.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def foreign_app(
    tmp_path_factory: pytest.TempPathFactory,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    app: StandProcess,
) -> Iterator[StandProcess]:
    """Инстанс aid над той же базой, у которого агент вкладки — чужой
    агент ACP без объявленного среза ветки."""
    workdir = tmp_path_factory.mktemp("acp-chat-foreign")
    config = StandConfig(
        workdir=workdir,
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        generation=GENERATION,
        agent_process={"command": sys.executable, "args": [str(FOREIGN_AGENT)]},
    )
    process = StandProcess(config=config, log_path=workdir / "foreign.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def sandboxed(
    tmp_path_factory: pytest.TempPathFactory,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    app: StandProcess,
) -> Iterator[StandProcess]:
    """Инстанс aid над той же базой с песочницей, как в бою: файлы
    вложений и инструментов — один workspace пользователя."""
    workdir = tmp_path_factory.mktemp("acp-chat-sandbox")
    config = StandConfig(
        workdir=workdir,
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        generation=GENERATION,
        sandbox=True,
    )
    process = StandProcess(config=config, log_path=workdir / "sandbox.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def ghosted(
    tmp_path_factory: pytest.TempPathFactory,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    app: StandProcess,
) -> Iterator[StandProcess]:
    """Инстанс aid над той же базой, профили которого называют сервер MCP,
    который не отвечает: на его порту никто не слушает."""
    workdir = tmp_path_factory.mktemp("acp-chat-ghost")
    ghost = {
        "limits": {"connect_timeout_sec": 5.0, "call_timeout_sec": 5.0},
        "endpoint": {
            "transport": "streamable-http",
            "service": {
                "scheme": "http",
                "host": StandUrl.HOST.value,
                "port": StandPorts().free(),
                "path": "",
            },
            "path": "/mcp",
        },
    }
    config = StandConfig(
        workdir=workdir,
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        generation=GENERATION,
        mcp_servers={"ghost": ghost},
    )
    process = StandProcess(config=config, log_path=workdir / "ghost.log")
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
    CHAT_START_STEP: ClassVar[str] = "on_chat_start"
    OWN_STEPS: ClassVar[tuple[str, ...]] = ("on_chat_start", "on_chat_resume")
    """Шаги самого chainlit вокруг обработчиков: в ленте треда их нет."""
    TASK_END_GRACE_SEC: ClassVar[float] = 0.5
    CACHE_KEY: ClassVar[str] = "chainlitKey"
    RESUMED: ClassVar[str] = "resume thread {thread}: session {session} is ready"
    """Строка лога приложения о конце возврата вкладки к треду."""

    def __init__(self, app: StandProcess, cookie: str, thread_id: str) -> None:
        self._app = app
        self._cookie = cookie
        self.thread_id = thread_id
        self.session_id = uuid4().hex
        self.events: list[tuple[str, Any]] = []
        self._task_ends = asyncio.Queue[None]()
        self._sio = socketio.AsyncClient(reconnection=False)
        self._sio.on("*", self._on_any)

    async def open(self, resumed: bool = False) -> None:
        """Подключение вкладки; resumed — тред уже есть, chainlit зовёт
        on_chat_resume и завершает его событием resume_thread."""
        logged = 0
        if self._app.log_path.is_file():
            logged = self._app.log_path.stat().st_size

        await self._connect()
        await self._sio.emit("connection_successful")
        # task_end на connection_successful приходит сразу; on_chat_start идёт
        # задачей со своим task_start/task_end вокруг шага run с его именем,
        # on_chat_resume заканчивается событием resume_thread
        await self.wait_task_end()
        if resumed:
            # история воспроизводится после отправки треда вкладке, её конец
            # приложение отмечает в логе
            await self.wait_event_index("resume_thread")
            await self._wait_resumed(logged)
            # on_chat_resume тоже обёрнут task_start/task_end: его конец не
            # должен сойти за конец следующего хода
            while not self._task_ends.empty():
                self._task_ends.get_nowait()

            return

        await self.wait_step_done(self.CHAT_START_STEP)
        await self.wait_task_end()

    async def reconnect(self) -> None:
        """Сокет вкладки вернулся после обрыва: chainlit восстанавливает ту же
        сессию по её id без on_chat_start и on_chat_resume."""
        await self._connect()
        await self._sio.emit("connection_successful")
        await self.wait_task_end()

    async def _connect(self) -> None:
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

    async def _wait_resumed(
        self, since: int, timeout_sec: float = TURN_TIMEOUT_SEC
    ) -> None:
        """Строка лога о конце возврата этой вкладки к треду после байта since."""
        marker = self.RESUMED.format(thread=self.thread_id, session=self.session_id)
        await self.wait_log(marker, since, timeout_sec)

    async def wait_log(
        self, marker: str, since: int = 0, timeout_sec: float = TURN_TIMEOUT_SEC
    ) -> None:
        """Строка лога приложения с marker после байта since."""
        expected = marker.encode()
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            with self._app.log_path.open("rb") as handle:
                handle.seek(since)
                for line in handle:
                    if expected in line:
                        return

            await asyncio.sleep(0.1)

        msg = f"no log line {marker!r} in {timeout_sec}s: {self._app.tail(40)}"
        raise AssertionError(msg)

    async def send(self, text: str, files: tuple[str, ...] = ()) -> str:
        """Вопрос пользователя с загруженными файлами files; итог — id его
        сообщения."""
        message = self._message(str(uuid4()), text)
        references: list[dict[str, str]] = []
        for file_id in files:
            references.append({"id": file_id})

        await self._sio.emit(
            "client_message", {"message": message, "fileReferences": references}
        )

        return str(message["id"])

    def upload(self, name: str, body: bytes, mime: str) -> str:
        """Файл вложением сессии вкладки, как его грузит интерфейс; итог — id
        файла для вопроса."""
        response = httpx.post(
            f"{self._app.config.base_url}/project/file",
            params={"session_id": self.session_id},
            files={"file": (name, body, mime)},
            headers={"Cookie": self._cookie},
            timeout=60.0,
        )
        assert response.status_code == 200, response.text[:300]

        return str(response.json()["id"])

    def action(self, name: str, payload: dict[str, Any]) -> Any:
        """Действие chainlit в сессии вкладки, как его шлёт интерфейс; итог —
        ответ обработчика действия."""
        body = {
            "sessionId": self.session_id,
            "action": {"name": name, "payload": payload, "id": str(uuid4())},
        }
        response = httpx.post(
            f"{self._app.config.base_url}/project/action",
            json=body,
            headers={"Cookie": self._cookie},
            timeout=60.0,
        )
        assert response.status_code == 200, response.text[:300]

        return response.json()["response"]

    async def edit(self, message_id: str, text: str) -> None:
        """Правка вопроса: тот же id, новый текст — как делает интерфейс."""
        await self._sio.emit(
            "edit_message", {"message": self._message(message_id, text)}
        )

    @staticmethod
    def _message(message_id: str, text: str) -> dict[str, Any]:
        return {
            "id": message_id,
            "name": "user",
            "type": "user_message",
            "output": text,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "metadata": {},
        }

    async def stop(self) -> None:
        await self._sio.emit("stop")

    async def wait_task_end(self, timeout_sec: float = TURN_TIMEOUT_SEC) -> None:
        """Конец хода: task_end шлют и обёртка chainlit своей вкладке, и лента
        всем вкладкам треда — второй, пришедший сразу за первым, не должен
        сойти за конец следующего хода."""
        await asyncio.wait_for(self._task_ends.get(), timeout_sec)
        await asyncio.sleep(self.TASK_END_GRACE_SEC)
        while not self._task_ends.empty():
            self._task_ends.get_nowait()

    async def wait_event(self, name: str, timeout_sec: float = TURN_TIMEOUT_SEC) -> Any:
        index = await self.wait_event_index(name, timeout_sec)

        return self.events[index][1]

    async def wait_event_index(
        self, name: str, timeout_sec: float = TURN_TIMEOUT_SEC, since: int = 0
    ) -> int:
        """Позиция первого события name начиная с since."""
        deadline = time.monotonic() + timeout_sec
        seen = since
        while time.monotonic() < deadline:
            for event, _ in self.events[seen:]:
                if event == name:
                    return seen

                seen += 1

            await asyncio.sleep(0.05)

        msg = f"event {name!r} did not arrive in {timeout_sec}s: {self.names()}"
        raise AssertionError(msg)

    async def wait_step_done(
        self, name: str, timeout_sec: float = TURN_TIMEOUT_SEC
    ) -> None:
        """update_message шага run с этим именем: обработчик chainlit закончился."""
        deadline = time.monotonic() + timeout_sec
        seen = 0
        while time.monotonic() < deadline:
            for event, payload in self.events[seen:]:
                seen += 1
                if event != "update_message":
                    continue

                if isinstance(payload, dict) and payload.get("name") == name:
                    return

            await asyncio.sleep(0.05)

        msg = f"step {name!r} did not finish in {timeout_sec}s: {self.names()}"
        raise AssertionError(msg)

    async def close(self) -> None:
        await self._sio.disconnect()

    async def end_session(self) -> None:
        """Конец сессии chainlit, как при новом чате: без clear_session сессия
        и её процесс живут до session_timeout ради реконнекта вкладки."""
        await self._sio.emit("clear_session")
        await asyncio.sleep(0.2)
        await self._sio.disconnect()

    def names(self) -> list[str]:
        return [event for event, _ in self.events]

    def trace(self) -> list[str]:
        """События с началом полезной нагрузки: для текста падения."""
        found: list[str] = []
        for event, payload in self.events:
            found.append(f"{event}: {str(payload)[:160]}")

        return found

    def frames(self) -> str:
        """Кадры ленты коротко: событие, тип и начало id шага."""
        lines: list[str] = []
        for event, payload in self.events:
            if not isinstance(payload, dict):
                lines.append(event)
                continue

            lines.append(
                f"{event} {payload.get('type', '')} {str(payload.get('id', ''))[:8]}"
            )

        return "\n".join(lines)

    def tokens(self) -> str:
        """Текст, пришедший стримом: chainlit открывает стрим шага кадром
        stream_start с первой пачкой в output, остальные пачки идут
        stream_token. Лента склеивает токены в пачки по окну, и короткий
        ответ может целиком прийти одним stream_start."""
        found: list[str] = []
        for event, payload in self.events:
            if event == "stream_start":
                found.append(str(payload.get("output", "")))

            if event == "stream_token":
                found.append(str(payload.get("token", "")))

        return "".join(found)

    def messages(
        self, kind: str = "assistant_message", since: int = 0
    ) -> list[dict[str, Any]]:
        """Кадры шагов вида kind, начиная с события номер since."""
        found: list[dict[str, Any]] = []
        for event, payload in self.events[since:]:
            if event not in ("new_message", "update_message", "stream_start"):
                continue

            if payload.get("type") == kind:
                found.append(payload)

        return found

    def errors(self, since: int = 0) -> list[str]:
        """Сбои во вкладке по порядку прихода, начиная с события since:
        пометки агента об ошибке в ленте, всплывающие уведомления и полоса
        недоступного агента."""
        found: list[str] = []
        for event, payload in self.events[since:]:
            if not isinstance(payload, dict):
                continue

            if event == "toast":
                found.append(str(payload.get("message")))
                continue

            if event == "agent_state":
                if not payload.get("available"):
                    found.append(f"{payload.get('text')} ({payload.get('detail')})")
                continue

            if event not in ("new_message", "update_message", "stream_start"):
                continue

            if payload.get("type") != "assistant_message":
                continue

            if payload.get("isError"):
                found.append(self._noticed(payload))

        return found

    def notices(self) -> list[str]:
        """Пометки агента в ленте с важностью предупреждения и сведения:
        текст и короткая причина."""
        found: list[str] = []
        for payload in self.messages("assistant_message"):
            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                continue

            if metadata.get(StepMeta.NOTICE.value) in ("warning", "info"):
                found.append(self._noticed(payload))

        return found

    @staticmethod
    def _noticed(payload: dict[str, Any]) -> str:
        """Текст блока пометки и его причина, как их видит человек."""
        text = str(payload.get("output"))
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return text

        detail = metadata.get(StepMeta.NOTICE_DETAIL.value)
        if not detail:
            return text

        return f"{text} ({detail})"

    def steps(self) -> dict[str, dict[str, Any]]:
        """Последняя нагрузка каждого шага ленты по id, кроме кружка ожидания."""
        found: dict[str, dict[str, Any]] = {}
        for event, payload in self.events:
            if event not in ("new_message", "update_message", "stream_start"):
                continue

            if not isinstance(payload, dict):
                continue

            if payload.get("output") == PULSE:
                continue

            if payload.get("name") in self.OWN_STEPS:
                continue

            found[str(payload.get("id"))] = payload

        return found

    def step_ids(self) -> dict[str, set[str]]:
        """Id шагов по типам: что лента нарисовала этой вкладке."""
        found: dict[str, set[str]] = {}
        for step_id, payload in self.steps().items():
            found.setdefault(str(payload.get("type")), set()).add(step_id)

        return found

    def elements(self) -> list[dict[str, Any]]:
        """Элементы, присланные вкладке: отдельно и вместе с тредом."""
        found: list[dict[str, Any]] = []
        for event, payload in self.events:
            if event == "element" and isinstance(payload, dict):
                found.append(payload)

            if event == "resume_thread" and isinstance(payload, dict):
                found.extend(payload.get("elements") or [])

        return found

    def attachments(self, step_id: str) -> list[dict[str, Any]]:
        """Элементы шага step_id по порядку номеров, как их нарисовала вкладка;
        без chainlitKey — ключа кеша файлов, своего у каждого инстанса."""
        found: list[dict[str, Any]] = []
        for element in self.elements():
            if element.get("forId") != step_id:
                continue

            shown = dict(element)
            shown.pop(self.CACHE_KEY, None)
            found.append(shown)

        return found

    async def wait_attachments(
        self, step_id: str, count: int, timeout_sec: float = TURN_TIMEOUT_SEC
    ) -> None:
        """Ждёт, пока у шага step_id не станет count элементов."""
        deadline = time.monotonic() + timeout_sec
        while len(self.attachments(step_id)) < count:
            if time.monotonic() > deadline:
                msg = (
                    f"step {step_id} of thread {self.thread_id} got "
                    f"{self.attachments(step_id)} instead of {count} elements in "
                    f"{timeout_sec}s: {self.names()}"
                )
                raise AssertionError(msg)

            await asyncio.sleep(0.05)

    async def _on_any(self, event: str, *args: Any) -> None:
        payload: Any = None
        if args:
            payload = args[0]

        self.events.append((event, payload))
        if event == "task_end":
            self._task_ends.put_nowait(None)


def _stopped_labels(texts: UiText) -> set[str]:
    """Подписи контейнера остановленного хода с любым разумным числом шагов."""
    labels: set[str] = set()
    for count in range(1, 20):
        labels.add(texts.text(StepText.STOPPED_STEPS.value, count=count))

    return labels


@pytest.fixture
async def tab(app: StandProcess, cookie: str) -> AsyncIterator[ChatSocket]:
    socket = ChatSocket(app, cookie, str(uuid4()))
    await socket.open()
    try:
        yield socket
    finally:
        await socket.close()


class AgentProcesses:
    """Процессы `boba --stdio`, запущенные стендом: по каталогу настроек в argv."""

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

    def of_thread(self, thread_id: str) -> dict[int, Path]:
        """Процессы агента треда: pid → файл настроек из его argv."""
        marker = str(self._agents_dir / f"thread-{thread_id}-")
        found: dict[int, Path] = {}
        for pid in self.pids():
            try:
                argv = ProcFiles(pid).cmdline()
            except OSError:
                continue

            for part in argv:
                if part.startswith(marker):
                    found[pid] = Path(part)

        return found

    async def await_thread(
        self, thread_id: str, other_than: int = 0, timeout_sec: float = 90.0
    ) -> int:
        """Pid единственного процесса агента треда, отличного от other_than:
        прежний процесс ушёл, новый поднялся."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            running = set(self.of_thread(thread_id))
            if len(running) == 1 and other_than not in running:
                return next(iter(running))

            await asyncio.sleep(0.3)

        msg = (
            f"thread {thread_id}: no single agent process other than {other_than} "
            f"in {timeout_sec}s, running {sorted(self.of_thread(thread_id))}"
        )
        raise AssertionError(msg)


class ChatRows:
    """Строки базы стенда: metadata треда и записи истории агента."""

    def __init__(
        self, postgres: PostgresConfig, schema: str, database: str, history_schema: str
    ) -> None:
        self._postgres = TestDatabase.config_of(postgres, database)
        self._schema = schema
        self._history_schema = history_schema

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

    async def history_rows(self, session_id: str) -> int:
        """Записи сессии в истории агента (схема [agent.history] стенда)."""
        pool = AsyncPostgresPool(self._postgres)
        await pool.open()
        try:
            async with pool.connection() as conn:
                cursor = await conn.execute(
                    sql.SQL(
                        "select count(*) from {}.history where session_id = %s"
                    ).format(sql.Identifier(self._history_schema)),
                    (session_id,),
                )
                row = await cursor.fetchone()
        finally:
            await pool.close()

        assert row is not None

        return int(row[0])

    async def thread_times(self, thread_id: str) -> tuple[datetime, datetime]:
        """created_at и updated_at треда."""
        pool = AsyncPostgresPool(self._postgres)
        await pool.open()
        try:
            async with pool.connection() as conn:
                cursor = await conn.execute(
                    sql.SQL(
                        "select created_at, updated_at from {}.threads where id = %s"
                    ).format(sql.Identifier(self._schema)),
                    (thread_id,),
                )
                row = await cursor.fetchone()
        finally:
            await pool.close()

        assert row is not None, f"no thread {thread_id}"

        return row[0], row[1]

    async def last_history_write(self, session_id: str) -> datetime:
        """Время последней записи сессии в истории агента."""
        pool = AsyncPostgresPool(self._postgres)
        await pool.open()
        try:
            async with pool.connection() as conn:
                cursor = await conn.execute(
                    sql.SQL(
                        "select max(recorded_at) from {}.history where session_id = %s"
                    ).format(sql.Identifier(self._history_schema)),
                    (session_id,),
                )
                row = await cursor.fetchone()
        finally:
            await pool.close()

        assert row is not None
        assert row[0] is not None, f"no history of session {session_id}"

        return row[0]


@pytest.fixture
def rows(
    stand_postgres: PostgresConfig,
    runtime_config: RuntimeConfig,
    stand_database: str,
    app: StandProcess[StandConfig],
) -> ChatRows:
    history_schema = app.config.agent_history["schema"]

    return ChatRows(
        stand_postgres, runtime_config.db_schema, stand_database, history_schema
    )


class TestConversation:
    """Вкладка стенда ведёт разговор через процесс агента."""

    async def test_turn_streams_the_answer_into_the_feed(
        self, app: StandProcess, tab: ChatSocket, rows: ChatRows, stand: Stand
    ) -> None:
        agents = AgentProcesses(app)
        # агенты вкладок прошлых тестов стенда ещё живы: агент этой вкладки
        # узнаётся по треду в его argv
        pid = await agents.await_thread(tab.thread_id)

        files = ProcFiles(pid)
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
        assert "answer the question" in tab.tokens(), (tab.trace(), app.tail(40))
        answers = tab.messages()
        assert answers, tab.trace()
        assert any("answer the question" in str(m.get("output")) for m in answers)

        meta = await rows.thread_meta(tab.thread_id)
        assert meta is not None
        assert meta.get(ThreadMetaField.AGENT_SESSION), meta
        assert meta.get(ThreadMetaField.CHAT_PROFILE) == PROFILE, meta

        complaints = app.complaints(since)
        assert not complaints, complaints

    async def test_the_tab_has_no_settings_and_the_agent_bypasses_permissions(
        self, app: StandProcess, tab: ChatSocket
    ) -> None:
        """Настройки агента задаёт только профиль: панели настроек вкладка не
        получает ни при старте, ни после хода, открывшего сессию, а файл
        настроек процесса несёт модель профиля и потолок allowBypass; сам
        режим bypassPermissions чат включает по ACP."""
        agents = AgentProcesses(app)
        pid = await agents.await_thread(tab.thread_id)

        await tab.send(f"plain {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()

        assert not tab.errors(), (tab.errors(), app.tail(80))
        assert "chat_settings" not in tab.names(), tab.trace()
        settings = agents.of_thread(tab.thread_id)[pid]
        body = json.loads(settings.read_text(encoding="utf-8"))
        assert body["permissions"] == {"allowBypass": True}
        assert body["model"] == PROFILE
        assert body["models"][PROFILE]["model"] == "fake-model-general"

    async def test_stop_cancels_the_turn(
        self, app: StandProcess, tab: ChatSocket, app_texts: UiText
    ) -> None:
        await tab.send(f"go {ScenarioName.LONG.value}")
        await tab.wait_event("stream_token")
        await tab.stop()
        await tab.wait_task_end()

        assert not tab.errors(), (tab.errors(), app.tail(80))
        labels = _stopped_labels(app_texts)
        containers = [s for s in tab.steps().values() if s.get("name") in labels]
        assert containers, tab.frames()
        note = app_texts.text(StepText.STOPPED.value)
        notes = [m for m in tab.messages() if note in str(m.get("output"))]
        assert not notes, "a turn with steps shows its stop in the container label"

    async def test_exited_agent_is_replaced_for_the_next_question(
        self, app: StandProcess, tab: ChatSocket
    ) -> None:
        """Процесс агента вкладки упал: следующий вопрос поднимает новый
        процесс на той же сессии и получает ответ."""
        agents = AgentProcesses(app)
        await tab.send(f"first {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(80))
        pid = await agents.await_thread(tab.thread_id)

        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and pid in agents.of_thread(tab.thread_id):
            await asyncio.sleep(0.2)

        mark = len(tab.events)
        await tab.send(f"second {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()

        assert not tab.errors(), (tab.errors(), app.tail(80))
        replaced = await agents.await_thread(tab.thread_id, other_than=pid)
        assert replaced != pid
        answers = tab.messages(since=mark)
        assert any("plain streamed answer" in str(m.get("output")) for m in answers), (
            tab.trace()[mark:],
            app.tail(60),
        )

    async def test_a_dropped_tab_lets_the_agent_go_and_gets_a_new_one_back(
        self, app: StandProcess, tab: ChatSocket
    ) -> None:
        """Сокет вкладки оборвался: сессия chainlit ждёт реконнекта, а процесс
        агента уходит сразу; вернувшаяся вкладка спрашивает в той же сессии,
        и отвечает новый процесс."""
        agents = AgentProcesses(app)
        await tab.send(f"first {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(80))
        pid = await agents.await_thread(tab.thread_id)

        await tab.close()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and pid in agents.of_thread(tab.thread_id):
            await asyncio.sleep(0.2)

        assert pid not in agents.of_thread(tab.thread_id), app.tail(60)

        await tab.reconnect()
        mark = len(tab.events)
        await tab.send(f"second {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()

        assert not tab.errors(), (tab.errors(), app.tail(80))
        replaced = await agents.await_thread(tab.thread_id, other_than=pid)
        assert replaced != pid
        answers = tab.messages(since=mark)
        assert any("plain streamed answer" in str(m.get("output")) for m in answers), (
            tab.trace()[mark:],
            app.tail(60),
        )

    async def test_closing_the_tab_ends_the_agent_and_removes_its_settings(
        self, app: StandProcess, cookie: str
    ) -> None:
        agents = AgentProcesses(app)
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        pid = await agents.await_thread(tab.thread_id)
        settings_dirs = [
            path
            for path in agents.settings_dirs()
            if (path / WorkdirFile.SETTINGS.value).is_file()
        ]
        assert settings_dirs

        await tab.end_session()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and pid in agents.pids():
            await asyncio.sleep(0.5)

        assert pid not in agents.pids(), app.tail(60)
        left = [
            path for path in agents.settings_dirs() if path.name.startswith("thread-")
        ]
        assert all((path / WorkdirFile.SETTINGS.value).is_file() for path in left)
        assert not any(str(pid) in str(path) for path in left)


class TestUnavailableServer:
    """Сервер MCP профиля не отвечает: агент открывает сессию без его
    инструментов и отвечает на вопрос, а лента хода показывает пометку с
    именем сервера и причиной; пометка — запись агента и видна и после
    возврата к треду."""

    async def test_agent_answers_while_a_server_of_the_profile_is_down(
        self, ghosted: StandProcess, cookie: str
    ) -> None:
        tab = ChatSocket(ghosted, cookie, str(uuid4()))
        await tab.open()
        try:
            await tab.send(f"hello {ScenarioName.THINKING_ANSWER.value}")
            await tab.wait_task_end()
            notes = tab.notices()
            answers: list[str] = []
            for payload in tab.messages():
                if payload.get("isError"):
                    continue

                answers.append(str(payload.get("output")))
        finally:
            await tab.end_session()

        answered = any("answer the question" in answer for answer in answers)
        assert answered, (tab.trace(), ghosted.tail(60))
        noted = [note for note in notes if "'ghost' isn't available" in note]
        assert noted, (notes, ghosted.tail(60))
        assert noted[0].endswith("(no connection)"), noted

        again = ChatSocket(ghosted, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            replayed = again.notices()
        finally:
            await again.end_session()

        assert noted[0] in replayed, (replayed, again.trace())


class TestHistory:
    """Возврат к треду, второй инстанс, правка вопроса, удаление треда."""

    async def test_resumed_tab_replays_the_history(
        self, app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        await tab.send(f"remember {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), tab.errors()
        meta = await rows.thread_meta(tab.thread_id)
        assert meta is not None
        session_id = meta[ThreadMetaField.AGENT_SESSION]
        assert await rows.history_rows(session_id) > 0
        await tab.end_session()

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            assert not again.errors(), (again.errors(), app.tail(60))
            replayed = again.messages("user_message")
            assert [m.get("output") for m in replayed] == [
                f"remember {ScenarioName.ANSWER.value}"
            ]
            assert any(
                "plain streamed answer" in str(m.get("output"))
                for m in again.messages()
            ), again.trace()

            await again.send(f"more {ScenarioName.THINKING.value}")
            await again.wait_task_end()
            assert not again.errors(), again.errors()
        finally:
            await again.end_session()

    async def test_resumed_tab_keeps_the_stop_and_the_failure_of_turns(
        self, app: StandProcess, cookie: str, app_texts: UiText
    ) -> None:
        """Подпись остановленного хода у контейнера и шаг сбоя модели —
        часть истории: вкладка, открывшая тред заново, получает те же шаги с
        тем же текстом, что видела живая."""
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        await tab.send(f"go {ScenarioName.LONG.value}")
        await tab.wait_event("stream_token")
        await tab.stop()
        await tab.wait_task_end()
        broken = f"broken {ScenarioName.ANSWER.value} {FailureName.STATUS_500.value}"
        await tab.send(broken)
        await tab.wait_task_end()
        live = tab.steps()
        await tab.end_session()

        labels = _stopped_labels(app_texts)
        marked: list[str] = []
        failed: list[str] = []
        for step_id, step in live.items():
            if step.get("name") in labels:
                marked.append(step_id)

            if step.get("isError"):
                failed.append(step_id)

        assert marked, tab.trace()
        assert failed, tab.trace()

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            replayed = again.steps()
            for step_id in [*marked, *failed]:
                assert step_id in replayed, (step_id, again.trace())
                shown = replayed[step_id].get("output")
                assert shown == live[step_id].get("output"), (step_id, shown)
                named = replayed[step_id].get("name")
                assert named == live[step_id].get("name"), (step_id, named)
                assert replayed[step_id].get("isError") == live[step_id].get("isError")
        finally:
            await again.end_session()

    async def test_second_instance_observes_and_asks_after_the_turn(
        self, app: StandProcess, second: StandProcess, cookie: str
    ) -> None:
        """Процесс вкладки первого инстанса после хода сессию не держит:
        вкладка второго инстанса видит историю и получает ответ на вопрос."""
        first = ChatSocket(app, cookie, str(uuid4()))
        await first.open()
        await first.send(f"start {ScenarioName.ANSWER.value}")
        await first.wait_task_end()
        assert not first.errors(), first.errors()

        try:
            observer = ChatSocket(second, cookie, first.thread_id)
            await observer.open(resumed=True)
            try:
                assert not observer.errors(), (observer.errors(), second.tail(60))
                assert [m.get("output") for m in observer.messages("user_message")] == [
                    f"start {ScenarioName.ANSWER.value}"
                ]

                mark = len(observer.events)
                await observer.send(f"mine {ScenarioName.ANSWER.value}")
                await observer.wait_task_end()
                assert not observer.errors(), (observer.errors(), second.tail(60))
                answers = " ".join(
                    str(m.get("output")) for m in observer.messages(since=mark)
                )
                assert "plain streamed answer" in answers, observer.trace()[mark:]
            finally:
                await observer.end_session()
        finally:
            await first.end_session()

    async def test_editing_a_question_forks_the_session(
        self, app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        sent = await tab.send(f"first {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), tab.errors()
        # шаг вопроса перепривязан к messageId, который вопросу дал агент
        question = str(tab.messages("user_message")[-1].get("id"))
        assert question != sent, tab.trace()
        before = await rows.thread_meta(tab.thread_id)
        assert before is not None
        original = before[ThreadMetaField.AGENT_SESSION]

        await tab.edit(question, f"edited {ScenarioName.THINKING_ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(60))
        after = await rows.thread_meta(tab.thread_id)
        assert after is not None
        branch = after[ThreadMetaField.AGENT_SESSION]
        assert branch != original
        # у треда одна сессия: прежняя удалена вместе с правкой
        assert await rows.history_rows(original) == 0
        assert await rows.history_rows(branch) > 0
        await tab.end_session()

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            assert not again.errors(), again.errors()
            assert [m.get("output") for m in again.messages("user_message")] == [
                f"edited {ScenarioName.THINKING_ANSWER.value}"
            ]
            answers = " ".join(str(m.get("output")) for m in again.messages())
            assert "answer the question" in answers, again.trace()
            assert "plain streamed answer" not in answers, again.trace()
        finally:
            await again.end_session()

    async def test_an_edit_of_a_busy_thread_changes_nothing(
        self, app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        """Тред ведёт идущий ход: правка вопроса отказана замком области до
        ветки — сессия агента и threads.meta прежние."""
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        try:
            await tab.send(f"first {ScenarioName.ANSWER.value}")
            await tab.wait_task_end()
            assert not tab.errors(), tab.errors()
            question = str(tab.messages("user_message")[-1].get("id"))
            before = await rows.thread_meta(tab.thread_id)
            assert before is not None
            original = before[ThreadMetaField.AGENT_SESSION]
            recorded = await rows.history_rows(original)

            mark = len(tab.events)
            await tab.send(f"long {ScenarioName.LONG.value}")
            await tab.wait_event_index("stream_token", since=mark)
            await tab.edit(question, f"edited {ScenarioName.ANSWER.value}")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not tab.errors():
                await asyncio.sleep(0.2)

            assert tab.errors(), tab.trace()
            assert "answering in another tab" in tab.errors()[-1], tab.errors()
            # конец обработчика правки, а не хода
            await tab.wait_task_end()
            during = await rows.thread_meta(tab.thread_id)
            assert during is not None
            assert during[ThreadMetaField.AGENT_SESSION] == original

            await tab.stop()
            await tab.wait_task_end()
            after = await rows.thread_meta(tab.thread_id)
            assert after is not None
            assert after[ThreadMetaField.AGENT_SESSION] == original
            assert await rows.history_rows(original) > recorded
        finally:
            await tab.end_session()

    async def test_a_tab_leaving_after_an_edit_keeps_the_thread_on_the_branch(
        self, app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        """Вкладка, открывшая тред до правки, на отключении не возвращает
        тред на прежнюю сессию: повторное открытие показывает ветку."""
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        await tab.send(f"first {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), tab.errors()
        question = str(tab.messages("user_message")[-1].get("id"))

        reader = ChatSocket(app, cookie, tab.thread_id)
        await reader.open(resumed=True)
        try:
            await tab.edit(question, f"edited {ScenarioName.THINKING_ANSWER.value}")
            await tab.wait_task_end()
            assert not tab.errors(), (tab.errors(), app.tail(60))
            edited = await rows.thread_meta(tab.thread_id)
            assert edited is not None
            branch = edited[ThreadMetaField.AGENT_SESSION]
        finally:
            await reader.end_session()

        await tab.end_session()
        left = await rows.thread_meta(tab.thread_id)
        assert left is not None
        assert left[ThreadMetaField.AGENT_SESSION] == branch

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            assert not again.errors(), again.errors()
            assert [m.get("output") for m in again.messages("user_message")] == [
                f"edited {ScenarioName.THINKING_ANSWER.value}"
            ]
        finally:
            await again.end_session()

    async def test_editing_after_a_reload_forks_the_session(
        self, app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        """Вопрос, воспроизведённый после перезагрузки треда, правится так же,
        как в живой вкладке: шаг вопроса есть в ленте, сообщения в chat_context
        chainlit у него нет."""
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        await tab.send(f"first {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), tab.errors()
        before = await rows.thread_meta(tab.thread_id)
        assert before is not None
        original = before[ThreadMetaField.AGENT_SESSION]
        await tab.end_session()

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            question = str(again.messages("user_message")[-1].get("id"))
            await again.edit(question, f"edited {ScenarioName.THINKING_ANSWER.value}")
            await again.wait_task_end()
            assert not again.errors(), (again.errors(), app.tail(60))
            after = await rows.thread_meta(tab.thread_id)
            assert after is not None
            assert after[ThreadMetaField.AGENT_SESSION] != original
        finally:
            await again.end_session()

        third = ChatSocket(app, cookie, tab.thread_id)
        await third.open(resumed=True)
        try:
            assert [m.get("output") for m in third.messages("user_message")] == [
                f"edited {ScenarioName.THINKING_ANSWER.value}"
            ]
        finally:
            await third.end_session()

    async def test_editing_a_question_asked_in_another_tab(
        self, app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        """Вопрос, который вкладка видела только живым ходом другой вкладки:
        её агент беседы с ним не загружал, правка отказана с советом
        перезагрузить тред, и вкладка перерисована из истории; после этого
        та же правка проходит."""
        asking = ChatSocket(app, cookie, str(uuid4()))
        await asking.open()
        await asking.send(f"zero {ScenarioName.ANSWER.value}")
        await asking.wait_task_end()
        assert not asking.errors(), asking.errors()

        editing = ChatSocket(app, cookie, asking.thread_id)
        await editing.open(resumed=True)
        try:
            mark = len(editing.events)
            await asking.send(f"one {ScenarioName.ANSWER.value}")
            await asking.wait_task_end()
            await editing.wait_task_end()
            live = editing.messages("user_message", mark)
            assert live, editing.trace()
            await asking.end_session()

            question = str(live[-1].get("id"))
            reloaded = len(editing.events)
            await editing.edit(question, f"edited {ScenarioName.ANSWER.value}")
            await editing.wait_task_end()
            refusals = editing.errors()
            assert refusals, editing.trace()
            assert "Reload the chat and try again" in refusals[-1], refusals
            await editing.wait_event_index("resume_thread", since=reloaded)

            await editing.edit(question, f"edited {ScenarioName.ANSWER.value}")
            await editing.wait_task_end()
            assert editing.errors() == refusals, (editing.errors(), app.tail(60))
        finally:
            await editing.end_session()

        again = ChatSocket(app, cookie, asking.thread_id)
        await again.open(resumed=True)
        try:
            questions = [m.get("output") for m in again.messages("user_message")]
            assert questions == [
                f"zero {ScenarioName.ANSWER.value}",
                f"edited {ScenarioName.ANSWER.value}",
            ], (questions, again.trace(), app.tail(80))
        finally:
            await again.end_session()

    async def test_a_refused_edit_redraws_the_editing_tab(
        self, app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        """Правка из вкладки, пока ход ведёт другая: отказ замком области,
        вкладка заново рисует беседу из истории, затем ошибка; ветки нет."""
        holder = ChatSocket(app, cookie, str(uuid4()))
        await holder.open()
        await holder.send(f"first {ScenarioName.ANSWER.value}")
        await holder.wait_task_end()
        assert not holder.errors(), holder.errors()
        before = await rows.thread_meta(holder.thread_id)
        assert before is not None

        editing = ChatSocket(app, cookie, holder.thread_id)
        await editing.open(resumed=True)
        try:
            question = str(editing.messages("user_message")[-1].get("id"))
            started = len(holder.events)
            await holder.send(f"long {ScenarioName.LONG.value}")
            await holder.wait_event_index("stream_token", since=started)

            mark = len(editing.events)
            await editing.edit(question, f"edited {ScenarioName.ANSWER.value}")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not editing.errors():
                await asyncio.sleep(0.2)

            assert editing.errors(), editing.trace()
            assert "answering in another tab" in editing.errors()[-1], editing.errors()
            redrawn = await editing.wait_event_index("resume_thread", since=mark)
            replayed = editing.messages("user_message", redrawn)
            assert replayed, editing.trace()[mark:]
            after = await rows.thread_meta(holder.thread_id)
            assert after == before

            await holder.stop()
            await holder.wait_task_end()
        finally:
            await editing.end_session()
            await holder.end_session()

    async def test_an_observer_edits_while_the_holder_tab_is_idle(
        self, app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        """Вкладка, задавшая вопрос, открыта, но хода не ведёт: её процесс
        сессию не держит, и правка из другой вкладки проходит сразу и
        удаляет прежнюю сессию."""
        holder = ChatSocket(app, cookie, str(uuid4()))
        await holder.open()
        await holder.send(f"first {ScenarioName.ANSWER.value}")
        await holder.wait_task_end()
        assert not holder.errors(), holder.errors()
        before = await rows.thread_meta(holder.thread_id)
        assert before is not None
        original = before[ThreadMetaField.AGENT_SESSION]
        recorded = await rows.history_rows(original)

        assert recorded > 0

        observer = ChatSocket(app, cookie, holder.thread_id)
        await observer.open(resumed=True)
        try:
            question = str(observer.messages("user_message")[-1].get("id"))
            await observer.edit(question, f"edited {ScenarioName.ANSWER.value}")
            await observer.wait_task_end()
            assert not observer.errors(), (observer.errors(), app.tail(60))
            after = await rows.thread_meta(holder.thread_id)
            assert after is not None
            assert after[ThreadMetaField.AGENT_SESSION] != original
            assert await rows.history_rows(original) == 0
        finally:
            await observer.end_session()
            await holder.end_session()

    async def test_deleting_an_edited_thread_deletes_all_its_sessions(
        self, app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        await tab.send(f"first {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        question = str(tab.messages("user_message")[-1].get("id"))
        before = await rows.thread_meta(tab.thread_id)
        assert before is not None
        await tab.edit(question, f"edited {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(60))
        after = await rows.thread_meta(tab.thread_id)
        assert after is not None
        sessions = {
            before[ThreadMetaField.AGENT_SESSION],
            after[ThreadMetaField.AGENT_SESSION],
        }

        response = httpx.request(
            "DELETE",
            f"{app.config.base_url}/project/thread",
            json={"threadId": tab.thread_id},
            headers={"Cookie": cookie},
            timeout=120.0,
        )
        assert response.status_code == 200, (response.text[:300], app.tail(60))
        for session_id in sessions:
            assert await rows.history_rows(session_id) == 0, session_id

        await tab.close()

    async def test_an_edit_redraws_the_other_tabs_without_the_tail(
        self, app: StandProcess, second: StandProcess, cookie: str
    ) -> None:
        """Правка в одной вкладке: вкладки треда на этом и на другом инстансе
        очищаются, перечитывают ветку без хвоста, затем видят новый ход; их
        агенты на ветке, а не на удалённой сессии, и следующий вопрос другой
        вкладки ведёт её процесс по ветке."""
        first = f"first {ScenarioName.ANSWER.value}"
        tail = f"tail {ScenarioName.ANSWER.value}"
        edited = f"edited {ScenarioName.ANSWER.value}"
        editing = ChatSocket(app, cookie, str(uuid4()))
        await editing.open()
        await editing.send(first)
        await editing.wait_task_end()
        await editing.send(tail)
        await editing.wait_task_end()
        assert not editing.errors(), editing.errors()
        asked = [
            m for m in editing.messages("user_message") if m.get("output") == first
        ]
        question = str(asked[-1].get("id"))

        other = ChatSocket(app, cookie, editing.thread_id)
        remote = ChatSocket(second, cookie, editing.thread_id)
        await other.open(resumed=True)
        await remote.open(resumed=True)
        try:
            marks = {other: len(other.events), remote: len(remote.events)}
            await editing.edit(question, edited)
            await editing.wait_task_end()
            assert not editing.errors(), (editing.errors(), app.tail(60))

            for tab, mark in marks.items():
                await tab.wait_task_end()
                cleared = await tab.wait_event_index("resume_thread", since=mark)
                assert tab.events[cleared][1].get("steps") == [], tab.trace()[mark:]
                shown = [m.get("output") for m in tab.messages("user_message", cleared)]
                assert tail not in shown, tab.trace()[mark:]
                assert first not in shown, tab.trace()[mark:]
                assert edited in shown, tab.trace()[mark:]

            mark = len(other.events)
            await other.send(f"mine {ScenarioName.ANSWER.value}")
            await other.wait_task_end()
            assert not other.errors(), (other.errors(), app.tail(60))
            shown = [m.get("output") for m in other.messages("user_message", mark)]
            assert f"mine {ScenarioName.ANSWER.value}" in shown, other.trace()[mark:]
        finally:
            await remote.end_session()
            await other.end_session()
            await editing.end_session()

    async def test_an_edit_without_the_cut_is_refused_and_changes_nothing(
        self, foreign_app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        """Агент вкладки не объявил срез ветки по messageId вопроса: правка
        отказана сообщением во вкладке, вкладка перерисована из истории,
        threads.meta прежний."""
        tab = ChatSocket(foreign_app, cookie, str(uuid4()))
        await tab.open()
        try:
            await tab.send("first question")
            await tab.wait_task_end()
            assert not tab.errors(), (tab.errors(), foreign_app.tail(60))
            question = str(tab.messages("user_message")[-1].get("id"))
            before = await rows.thread_meta(tab.thread_id)
            assert before is not None
            assert before.get(ThreadMetaField.AGENT_SESSION), before

            mark = len(tab.events)
            await tab.edit(question, "edited question")
            await tab.wait_task_end()
            refusals = tab.errors()
            assert refusals, tab.trace()[mark:]
            assert "can't branch a conversation" in refusals[-1], refusals
            await tab.wait_event_index("resume_thread", since=mark)
            assert await rows.thread_meta(tab.thread_id) == before
        finally:
            await tab.end_session()

    async def test_notice_of_the_agent_reaches_the_feed(
        self, foreign_app: StandProcess, cookie: str
    ) -> None:
        """Пометка notice агента об ошибке видна во вкладке, а не теряется
        среди служебных уведомлений."""
        tab = ChatSocket(foreign_app, cookie, str(uuid4()))
        await tab.open()
        try:
            await tab.send("notice: the history store is unreachable")
            await tab.wait_task_end()

            shown = tab.errors()
            assert any("the history store is unreachable" in e for e in shown), (
                tab.trace(),
                foreign_app.tail(60),
            )
        finally:
            await tab.end_session()

    async def test_stop_on_a_lost_agent_connection_is_shown_in_the_feed(
        self, foreign_app: StandProcess, cookie: str
    ) -> None:
        """Агент закрыл соединение посреди хода: кнопка Stop не может
        отправить ему session/cancel, и отказ виден во вкладке, а не глохнет
        в обработчике chainlit."""
        tab = ChatSocket(foreign_app, cookie, str(uuid4()))
        await tab.open()
        try:
            await tab.send("detach")
            await tab.wait_event("new_message")
            await asyncio.sleep(1.0)
            mark = len(tab.events)
            await tab.stop()

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not tab.errors():
                await asyncio.sleep(0.2)

            shown = tab.errors()
            assert any("session/cancel" in e for e in shown), (
                tab.trace()[mark:],
                foreign_app.tail(60),
            )
        finally:
            await tab.end_session()

    @pytest.mark.parametrize("keep_open", [False, True], ids=["closed", "open"])
    async def test_deleting_the_thread_deletes_its_files(
        self, app: StandProcess, cookie: str, keep_open: bool
    ) -> None:
        """Файл вложения треда после удаления треда не отдаётся: область треда
        удалена у сервиса файлов от имени владельца — по строке users, когда
        вкладка закрыта, и по живой сессии, когда открыта. Таблицу панель не
        показывает: вложение — файл со ссылкой на скачивание."""
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        file_id = tab.upload("gone.csv", b"gone,with\nthe,thread\n", "text/csv")
        await tab.send(f"files {ScenarioName.ANSWER.value}", (file_id,))
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(60))
        question = str(tab.messages("user_message")[-1].get("id"))
        attached = [e for e in tab.elements() if e.get("forId") == question]
        assert attached, tab.trace()
        address = StandUrl.of(app.config.app_port, str(attached[-1].get("url")))
        served = httpx.get(address, headers={"Cookie": cookie}, timeout=30.0)
        assert served.status_code == 200, served.text[:200]
        if not keep_open:
            await tab.end_session()

        try:
            response = httpx.request(
                "DELETE",
                f"{app.config.base_url}/project/thread",
                json={"threadId": tab.thread_id},
                headers={"Cookie": cookie},
                timeout=120.0,
            )
            assert response.status_code == 200, (response.text[:300], app.tail(60))
            gone = httpx.get(address, headers={"Cookie": cookie}, timeout=30.0)
            assert gone.status_code == 404, (gone.status_code, gone.text[:200])
        finally:
            if keep_open:
                await tab.end_session()

    async def test_question_attachment_lives_in_the_agent_history(
        self, app: StandProcess, cookie: str, llm_port: int
    ) -> None:
        """Вложение вопроса после перезагрузки: текст вопроса без ссылки,
        файл без вьювера панели (таблица) — элемент шага вопроса с адресом
        маршрута canvas, он скачивается; правка вопроса после перезагрузки
        сохраняет вложение для модели."""
        text = f"attach {ScenarioName.ANSWER.value}"
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        file_id = tab.upload("note.csv", b"attached text", "text/csv")
        await tab.send(text, (file_id,))
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(60))
        await tab.end_session()

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            asked = again.messages("user_message")
            assert [m.get("output") for m in asked] == [text]
            question = str(asked[-1].get("id"))
            attached = [e for e in again.elements() if e.get("forId") == question]
            assert attached, again.trace()
            url = str(attached[-1].get("url"))
            assert attached[-1].get("name") == "note.csv"
            assert url.startswith(
                f"{app.config.url_prefix}/canvas/{tab.thread_id}/upload/"
            ), url
            served = httpx.get(
                StandUrl.of(app.config.app_port, url),
                headers={"Cookie": cookie},
                timeout=30.0,
            )
            assert served.status_code == 200, served.text[:200]
            assert served.content == b"attached text"

            edited = f"edited-{uuid4().hex} {ScenarioName.ANSWER.value}"
            await again.edit(question, edited)
            await again.wait_task_end()
            assert not again.errors(), (again.errors(), app.tail(60))
            request = LlmRequests(llm_port).with_text(edited)
            assert "[@note.csv](" in json.dumps(request["messages"])
        finally:
            await again.end_session()

    async def test_deleting_the_thread_deletes_the_agent_session(
        self, app: StandProcess, cookie: str, rows: ChatRows
    ) -> None:
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        await tab.send(f"bye {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), tab.errors()
        meta = await rows.thread_meta(tab.thread_id)
        assert meta is not None
        session_id = meta[ThreadMetaField.AGENT_SESSION]
        assert await rows.history_rows(session_id) > 0

        response = httpx.request(
            "DELETE",
            f"{app.config.base_url}/project/thread",
            json={"threadId": tab.thread_id},
            headers={"Cookie": cookie},
            timeout=120.0,
        )
        assert response.status_code == 200, (response.text[:300], app.tail(60))

        assert await rows.thread_meta(tab.thread_id) is None
        assert await rows.history_rows(session_id) == 0
        await tab.close()


class TestQueuedQuestions:
    """Вопрос во время хода уходит в очередь сессии агента: ход не
    прерывается, вопрос получает свой ход агента после текущего ответа;
    остановка хода снимает его и возвращает текст в поле ввода."""

    HELD: ClassVar[str] = "Describe the migration steps slowly"
    HELD_ANSWER: ClassVar[str] = "Copy the schema, move the data and compare counts."
    QUEUED: ClassVar[str] = "And how long will it take?"
    QUEUED_ANSWER: ClassVar[str] = "About an hour on the stand."
    LATER: ClassVar[str] = "Then check the indexes"
    HELD_TOKENS: ClassVar[int] = 3

    def _book(self, llm_port: int) -> ReplyBookClient:
        book = ReplyBookClient(StandUrl.of(llm_port))
        book.register(
            [
                Reply(
                    question=self.HELD,
                    turns=[
                        ReplyTurn(
                            content=self.HELD_ANSWER,
                            hold=True,
                            hold_after_tokens=self.HELD_TOKENS,
                        )
                    ],
                ),
                Reply(
                    question=self.QUEUED,
                    turns=[ReplyTurn(content=self.QUEUED_ANSWER)],
                ),
            ]
        )

        return book

    def _questions(self, tab: ChatSocket) -> list[tuple[str, str]]:
        """Вопросы ленты по порядку появления: id шага и текст."""
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        for payload in tab.messages("user_message"):
            step_id = str(payload.get("id"))
            if step_id in seen:
                continue

            seen.add(step_id)
            found.append((step_id, str(payload.get("output"))))

        return found

    def _deleted(self, tab: ChatSocket) -> set[str]:
        found: set[str] = set()
        for event, payload in tab.events:
            if event == "delete_message" and isinstance(payload, dict):
                found.add(str(payload.get("id")))

        return found

    def _restored(self, tab: ChatSocket) -> list[list[str]]:
        found: list[list[str]] = []
        for event, payload in tab.events:
            if event != "window_message" or not isinstance(payload, dict):
                continue

            if payload.get("type") == SignalType.COMPOSER_RESTORE:
                found.append(list(payload["texts"]))

        return found

    async def test_a_question_during_the_turn_waits_for_its_own_turn(
        self, app: StandProcess, cookie: str, llm_port: int
    ) -> None:
        book = self._book(llm_port)
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        try:
            await tab.send(self.HELD)
            await asyncio.to_thread(book.await_held, self.HELD, TURN_TIMEOUT_SEC)
            sent = await tab.send(self.QUEUED)
            await tab.wait_log(f"question {sent} goes to the queue")
            assert self.HELD in book.held(), app.tail(40)
            assert not self._questions(tab)[1:], tab.frames()

            book.release(self.HELD)
            await tab.wait_task_end()

            assert not tab.errors(), (tab.errors(), app.tail(80))
            assert sent in self._deleted(tab), tab.frames()
            live = self._questions(tab)
            texts = [text for _, text in live]
            assert texts == [self.HELD, self.QUEUED], tab.frames()
            assert sent not in {step_id for step_id, _ in live}, live
            answers = [str(m.get("output")) for m in tab.messages()]
            held_at = next(i for i, a in enumerate(answers) if self.HELD_ANSWER in a)
            queued_at = next(
                i for i, a in enumerate(answers) if self.QUEUED_ANSWER in a
            )
            assert held_at < queued_at, answers
            live_steps = tab.step_ids()
        finally:
            await tab.end_session()

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            assert not again.errors(), (again.errors(), app.tail(60))
            assert self._questions(again) == live, again.frames()
            replayed = again.step_ids()
            for kind in ("user_message", "assistant_message"):
                assert live_steps.get(kind) == replayed.get(kind), (
                    kind,
                    again.frames(),
                )
        finally:
            await again.end_session()

    async def test_stop_withdraws_queued_questions_back_to_the_input(
        self, app: StandProcess, cookie: str, llm_port: int
    ) -> None:
        book = self._book(llm_port)
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        try:
            await tab.send(self.HELD)
            await asyncio.to_thread(book.await_held, self.HELD, TURN_TIMEOUT_SEC)
            first = await tab.send(self.QUEUED)
            second = await tab.send(self.LATER)
            await tab.wait_log(f"question {second} goes to the queue")
            await tab.stop()
            await tab.wait_task_end()
            book.release(self.HELD)

            assert not tab.errors(), (tab.errors(), app.tail(80))
            assert self._restored(tab) == [[self.QUEUED, self.LATER]], tab.trace()
            assert {first, second} <= self._deleted(tab), tab.frames()
            texts = [text for _, text in self._questions(tab)]
            assert texts == [self.HELD], tab.frames()

            mark = len(tab.events)
            await tab.send(f"again {ScenarioName.ANSWER.value}")
            await tab.wait_task_end()
            assert not tab.errors(), (tab.errors(), app.tail(80))
            assert tab.messages(since=mark), tab.trace()[mark:]
        finally:
            await tab.end_session()


class TestThreadActivity:
    """Тред встаёт в списке по последней записи истории агента: ход
    сдвигает threads.updated_at ко времени последней записи, и вкладки
    владельца — та, что ведёт ход, и вкладка другого треда на этом или
    втором инстансе — получают это значение сигналом страницы; возврат к
    треду время не трогает."""

    @pytest.fixture(params=["same", "second"])
    def watcher_app(
        self, request: pytest.FixtureRequest, app: StandProcess
    ) -> StandProcess:
        """Инстанс вкладки, которая следит за списком: тот же или второй."""
        if request.param == "same":
            return app

        return request.getfixturevalue("second")

    @staticmethod
    def _activity(tab: ChatSocket, thread_id: str, since: int = 0) -> list[datetime]:
        stamps: list[datetime] = []
        for event, payload in tab.events[since:]:
            if event != "window_message" or not isinstance(payload, dict):
                continue

            if payload.get("type") != SignalType.THREAD_ACTIVITY:
                continue

            if payload.get("threadId") != thread_id:
                continue

            stamps.append(datetime.fromisoformat(str(payload["updatedAt"])))

        return stamps

    async def _await_activity(
        self, tab: ChatSocket, thread_id: str, rows: ChatRows, session_id: str
    ) -> tuple[datetime, list[datetime]]:
        """Последняя запись истории сессии и сигналы активности треда во
        вкладке, когда последний из них дошёл до этой записи."""
        deadline = time.monotonic() + TURN_TIMEOUT_SEC
        while time.monotonic() < deadline:
            last = await rows.last_history_write(session_id)
            stamps = self._activity(tab, thread_id)
            if stamps and stamps[-1] == last:
                return last, stamps

            await asyncio.sleep(0.1)

        msg = (
            f"no activity signal of thread {thread_id} up to the last history "
            f"write in {TURN_TIMEOUT_SEC}s: {self._activity(tab, thread_id)}"
        )
        raise AssertionError(msg)

    async def test_a_turn_moves_the_thread_to_its_last_history_write(
        self,
        app: StandProcess,
        watcher_app: StandProcess,
        cookie: str,
        rows: ChatRows,
    ) -> None:
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        watcher = ChatSocket(watcher_app, cookie, str(uuid4()))
        await watcher.open()
        try:
            await tab.send(f"activity probe {ScenarioName.ANSWER.value}")
            await tab.wait_task_end()
            assert not tab.errors(), (tab.errors(), app.tail(60))

            meta = await rows.thread_meta(tab.thread_id)
            assert meta is not None
            session_id = meta[ThreadMetaField.AGENT_SESSION]
            last, seen = await self._await_activity(
                watcher, tab.thread_id, rows, session_id
            )
            _, own = await self._await_activity(tab, tab.thread_id, rows, session_id)
            assert seen == sorted(seen)
            assert own[-1] == last

            created_at, updated_at = await rows.thread_times(tab.thread_id)
            assert updated_at == last
            assert updated_at > created_at
        finally:
            await tab.end_session()

        mark = len(watcher.events)
        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            assert not again.errors(), (again.errors(), app.tail(60))
            assert await rows.thread_times(tab.thread_id) == (created_at, updated_at)
            assert self._activity(watcher, tab.thread_id, mark) == []
        finally:
            await again.end_session()
            await watcher.end_session()


class TestTabsOfOneThread:
    """Две вкладки одного треда — на одном инстансе или на двух над общей
    базой и шиной: ход ведёт вкладка, которая спросила, а процесс агента
    прошлого хода сессию между ходами не держит; вопрос во время чужого хода
    отказан занятым тредом."""

    ANSWERED: ClassVar[str] = "plain streamed answer"

    @pytest.fixture(params=["same", "second"])
    def other_app(
        self, request: pytest.FixtureRequest, app: StandProcess
    ) -> StandProcess:
        """Инстанс второй вкладки: тот же, что у первой, или второй."""
        if request.param == "same":
            return app

        return request.getfixturevalue("second")

    async def _reply(self, tab: ChatSocket, since: int) -> list[str]:
        """Ждёт после события since ответ или отказ хода вкладки; итог —
        отказы, пришедшие за это время."""
        deadline = time.monotonic() + TURN_TIMEOUT_SEC
        while time.monotonic() < deadline:
            refusals = tab.errors(since)
            answered = False
            for payload in tab.messages(since=since):
                if self.ANSWERED in str(payload.get("output")):
                    answered = True

            if refusals:
                return refusals

            if answered:
                return []

            await asyncio.sleep(0.1)

        msg = f"no reply in {TURN_TIMEOUT_SEC}s: {tab.trace()[since:]}"
        raise AssertionError(msg)

    async def test_a_tab_stops_the_turn_of_another_and_asks_about_a_file(
        self, app: StandProcess, other_app: StandProcess, cookie: str, llm_port: int
    ) -> None:
        """Вопрос в первой вкладке, Stop и вопрос с файлами во второй: ход
        второй вкладки идёт, модель получает ссылки на файлы, первая вкладка
        рисует вложения вопроса так же, как вторая: csv — скачиванием, txt —
        карточкой канваса."""
        first = ChatSocket(app, cookie, str(uuid4()))
        await first.open()
        other = ChatSocket(other_app, cookie, first.thread_id)
        try:
            await first.send(f"go {ScenarioName.LONG.value}")
            await first.wait_event("stream_token")
            await other.open(resumed=True)

            await other.stop()
            await first.wait_task_end()

            mark = len(other.events)
            table = other.upload(
                "note.csv", b"the,file\nof,the second tab\n", "text/csv"
            )
            note = other.upload("note.txt", b"shown text", ContentMime.PLAIN.value)
            text = f"what is in this file {uuid4().hex}? {ScenarioName.ANSWER.value}"
            await other.send(text, (table, note))
            refusals = await self._reply(other, mark)

            assert not refusals, (refusals, app.tail(60), other_app.tail(60))
            request = LlmRequests(llm_port).with_text(text)
            assert "[@note.csv](" in json.dumps(request["messages"])
            assert "[@note.txt](" in json.dumps(request["messages"])

            asked = other.messages("user_message", since=mark)[-1]
            question = str(asked.get("id"))
            drawn = other.attachments(question)
            names: list[str] = []
            for element in drawn:
                names.append(str(element.get("name")))

            assert names == ["note.csv", CanvasElement.LINK.value], drawn

            await first.wait_attachments(question, len(drawn))
            seen = self._in_thread(first.attachments(question), first.thread_id)
            sent = self._in_thread(drawn, other.thread_id)
            assert seen == sent, json.dumps([seen, sent], indent=1)
        finally:
            await other.end_session()
            await first.end_session()

    @staticmethod
    def _in_thread(
        elements: list[dict[str, Any]], thread_id: str
    ) -> list[dict[str, Any]]:
        """Элементы с путём карточки от каталога треда: у второго инстанса
        стенда свой boba-mcp, и workspace сервера файлов у него другой."""
        found: list[dict[str, Any]] = []
        for element in elements:
            shown = dict(element)
            props = shown.get("props")
            if not isinstance(props, dict):
                found.append(shown)
                continue

            path = props.get("path")
            if path is None:
                found.append(shown)
                continue

            parts = PurePosixPath(str(path)).parts
            at = parts.index(thread_id)
            tail = PurePosixPath(*parts[at:])
            shown["props"] = {**props, "path": str(tail)}
            found.append(shown)

        return found

    async def test_a_question_during_the_turn_of_another_tab_is_refused(
        self, app: StandProcess, other_app: StandProcess, cookie: str
    ) -> None:
        """Вопрос второй вкладки во время хода первой отказан занятым тредом;
        после конца хода тот же вопрос проходит, затем первая вкладка снова
        спрашивает сама."""
        first = ChatSocket(app, cookie, str(uuid4()))
        await first.open()
        other = ChatSocket(other_app, cookie, first.thread_id)
        try:
            await first.send(f"go {ScenarioName.LONG.value}")
            await first.wait_event("stream_token")
            await other.open(resumed=True)

            busy = len(other.events)
            await other.send(f"mine {ScenarioName.ANSWER.value}")
            refusals = await self._reply(other, busy)
            assert refusals, other.trace()[busy:]
            assert "answering in another tab" in refusals[-1], refusals

            await first.stop()
            await first.wait_task_end()

            mark = len(other.events)
            await other.send(f"mine {ScenarioName.ANSWER.value}")
            refused = await self._reply(other, mark)
            assert not refused, (refused, app.tail(60), other_app.tail(60))

            back = len(first.events)
            await first.send(f"back {ScenarioName.ANSWER.value}")
            again = await self._reply(first, back)
            assert not again, (again, app.tail(60), other_app.tail(60))
        finally:
            await other.end_session()
            await first.end_session()


class TestFeed:
    """Лента из уведомлений агента: история и живой ход — одни шаги; второй
    инстанс видит токены чужого хода; элементы и оценки привязаны к шагам."""

    @staticmethod
    def _answer_id(tab: ChatSocket) -> str:
        """Id первого ответа хода: ответ вопроса без номера."""
        answers = [
            step_id
            for step_id, payload in tab.steps().items()
            if payload.get("type") == "assistant_message"
            and not payload.get("isError")
            and payload.get("output")
        ]
        assert answers, tab.trace()

        return answers[0]

    @staticmethod
    def _shown(tab: ChatSocket) -> dict[str, tuple[str, ...]]:
        """Последний кадр element каждого элемента шага ленты по id: имя,
        адрес, шаг, props, вид и показ. Элемент оптимистичного шага вопроса,
        который фронт рисует сам до ответа агента, в ленту треда не входит."""
        steps = tab.steps()
        found: dict[str, tuple[str, ...]] = {}
        for element in tab.elements():
            if str(element.get("forId")) not in steps:
                continue

            found[str(element.get("id"))] = (
                str(element.get("name")),
                str(element.get("url")),
                str(element.get("forId")),
                json.dumps(element.get("props"), sort_keys=True),
                str(element.get("type")),
                str(element.get("display")),
            )

        return found

    async def test_elements_of_a_turn_are_the_same_after_a_reload(
        self, sandboxed: StandProcess, cookie: str
    ) -> None:
        """Вложение вопроса, файл итога send_file и ссылка панели canvas_open
        после перезагрузки треда приходят теми же кадрами element, что вживую.
        Текст показывает панель: вложение — кнопка открытия в панели с путём
        файла в workspace, как у файлов итогов."""
        app = sandboxed
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        file_id = tab.upload("note.txt", b"shown text", "text/plain")
        await tab.send(f"attach {ScenarioName.ANSWER.value}", (file_id,))
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(60))
        question = str(tab.messages("user_message")[-1].get("id"))
        attached = [e for e in tab.elements() if e.get("forId") == question]
        assert attached, tab.trace()
        assert attached[-1].get("name") == "CanvasLink", attached
        path = str(attached[-1]["props"]["path"])
        assert path == f"/workspace/{tab.thread_id}/upload/note.txt", path
        tools = StandToolNames()
        calls = [
            {"name": tools.full("send_file"), "arguments": {"path": path}},
            {"name": tools.full("canvas_open"), "arguments": {"path": path}},
        ]
        await tab.send(f"{ScenarioName.CALL.value} {json.dumps(calls)}")
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(60))
        live = self._shown(tab)
        await tab.end_session()

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            assert not again.errors(), (again.errors(), app.tail(60))
            replayed = self._shown(again)
        finally:
            await again.end_session()

        shown: list[str] = []
        for name, *_ in live.values():
            shown.append(name)

        # вложение вопроса и ссылки на файл итога у обоих вызовов (у текста
        # есть вьювер канваса) и кнопки журналов вызовов
        assert sorted(shown) == [
            "CanvasLink",
            "CanvasLink",
            "CanvasLink",
            "CanvasStream",
            "CanvasStream",
        ], live
        assert live == replayed, (live, replayed)

    async def test_text_attachments_of_a_question_open_in_the_panel(
        self, app: StandProcess, cookie: str
    ) -> None:
        """Вложения вопроса, которые показывает панель (текст и markdown):
        ход не падает, у шага вопроса — карточки канваса с путями файлов в
        workspace сервера, клик по карточке открывает панель с файлом."""
        text = f"attach {ScenarioName.ANSWER.value}"
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        try:
            note = tab.upload("note.txt", b"shown text", ContentMime.PLAIN.value)
            readme = tab.upload("readme.md", b"# shown", ContentMime.MARKDOWN.value)
            await tab.send(text, (note, readme))
            await tab.wait_task_end()
            assert not tab.errors(), (tab.errors(), app.tail(60))

            asked = tab.messages("user_message")[-1]
            assert asked.get("output") == text, asked
            question = str(asked.get("id"))
            assert question in tab.steps(), tab.trace()

            cards: dict[str, dict[str, Any]] = {}
            for element in tab.elements():
                if element.get("forId") != question:
                    continue

                assert element.get("name") == CanvasElement.LINK.value, element
                cards[str(element["props"]["label"])] = element["props"]

            assert sorted(cards) == ["note.txt", "readme.md"], tab.trace()

            shown = await self._panel(tab, cards["note.txt"])
            assert shown.get("kind") == CanvasKind.STREAM.value, shown
            assert shown.get("text") == "shown text", shown

            shown = await self._panel(tab, cards["readme.md"])
            assert shown.get("kind") == CanvasKind.TEXT.value, shown
            assert not tab.errors(), (tab.errors(), app.tail(60))
        finally:
            await tab.end_session()

    @staticmethod
    async def _panel(tab: ChatSocket, card: dict[str, Any]) -> dict[str, Any]:
        """Клик по карточке канваса, как его шлёт фронт: панель открывается
        элементом CanvasView с файлом карточки, путь которого — под каталогом
        треда в workspace; итог — содержимое панели."""
        path = str(card["path"])
        assert path.endswith(f"/{tab.thread_id}/upload/{card['label']}"), card

        since = len(tab.events)
        tab.action(CanvasAction.OPEN.value, {"path": path, "mime": str(card["mime"])})
        at = await tab.wait_event_index("element", since=since)
        shown = tab.events[at][1]
        assert shown.get("name") == CanvasElement.VIEW.value, tab.trace()
        assert shown.get("display") == "side", shown
        assert shown["props"].get("path") == path, shown

        return shown["props"]

    async def test_resumed_tab_draws_the_same_steps_as_the_live_turn(
        self, app: StandProcess, cookie: str
    ) -> None:
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        await tab.send(f"same {ScenarioName.THINKING_ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), (tab.errors(), app.tail(60))
        live = tab.step_ids()
        live_steps = tab.steps()
        await tab.end_session()

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            assert not again.errors(), (again.errors(), app.tail(60))
            replayed = again.step_ids()
            for kind in ("user_message", "run", "llm", "assistant_message"):
                assert live.get(kind) == replayed.get(kind), (kind, live, replayed)

            replayed_steps = again.steps()
            final = {
                str(p.get("id"))
                for e, p in again.events
                if e in ("new_message", "update_message") and isinstance(p, dict)
            }
            assert live["llm"] <= final, again.frames()
            assert live["assistant_message"] <= final, again.frames()
            for step_id in live["assistant_message"] | live["llm"]:
                assert live_steps[step_id].get("output") == replayed_steps[step_id].get(
                    "output"
                ), step_id
                assert live_steps[step_id].get("parentId") == replayed_steps[
                    step_id
                ].get("parentId"), step_id
        finally:
            await again.end_session()

    FOLLOWED: ClassVar[str] = "Follow this turn from the second instance"
    FOLLOWED_REASONING: ClassVar[str] = (
        "the holder streams a few words while the observer joins the thread"
    )
    FOLLOWED_ANSWER: ClassVar[str] = (
        "Result. The observer joined a live turn and saw its tokens and its end."
    )

    async def test_second_instance_follows_a_live_turn(
        self, app: StandProcess, second: StandProcess, cookie: str, llm_port: int
    ) -> None:
        # ход держится живым, пока наблюдатель не подключится: иначе под
        # нагрузкой он кончается раньше, чем второй инстанс подхватит тред
        book = ReplyBookClient(StandUrl.of(llm_port))
        turn = ReplyTurn(
            reasoning=self.FOLLOWED_REASONING,
            content=self.FOLLOWED_ANSWER,
            hold=True,
            hold_after_tokens=1,
        )
        followed = Reply(
            question=self.FOLLOWED, turns=[turn], token_delay_sec=TOKEN_DELAY_SEC
        )
        book.register([followed])

        holder = ChatSocket(app, cookie, str(uuid4()))
        await holder.open()
        observer = ChatSocket(second, cookie, holder.thread_id)
        try:
            await holder.send(self.FOLLOWED)
            await asyncio.to_thread(book.await_held, self.FOLLOWED, TURN_TIMEOUT_SEC)
            # удержанный поток отдал первый токен: шаг открыт им, токенов нет
            await holder.wait_event("stream_start")

            await observer.open(resumed=True)
            book.release(self.FOLLOWED)
            await observer.wait_event("stream_token", timeout_sec=60.0)
            await observer.wait_task_end()
            await holder.wait_task_end()

            assert not observer.errors(), (observer.errors(), second.tail(60))
            assert "Result." in observer.tokens() + "".join(
                str(m.get("output")) for m in observer.messages()
            ), observer.trace()
            assert (
                holder.step_ids()["assistant_message"]
                <= (observer.step_ids()["assistant_message"])
            ), (holder.step_ids(), observer.step_ids())
        finally:
            book.release(self.FOLLOWED)
            await observer.end_session()
            await holder.end_session()

    async def test_feedback_is_restored_on_resume(
        self, app: StandProcess, cookie: str
    ) -> None:
        tab = ChatSocket(app, cookie, str(uuid4()))
        await tab.open()
        await tab.send(f"rate {ScenarioName.ANSWER.value}")
        await tab.wait_task_end()
        assert not tab.errors(), tab.errors()
        answer_id = self._answer_id(tab)

        response = httpx.put(
            f"{app.config.base_url}/feedback",
            json={
                "feedback": {
                    "forId": answer_id,
                    "value": 1,
                    "comment": "good",
                    "threadId": tab.thread_id,
                },
                "sessionId": tab.session_id,
            },
            headers={"Cookie": cookie},
            timeout=30.0,
        )
        assert response.status_code == 200, response.text[:300]
        await tab.end_session()

        again = ChatSocket(app, cookie, tab.thread_id)
        await again.open(resumed=True)
        try:
            await again.wait_event("resume_thread")
            rated = [
                payload
                for event, payload in again.events
                if event == "update_message"
                and isinstance(payload, dict)
                and payload.get("id") == answer_id
                and payload.get("feedback")
            ]
            if not rated:
                await asyncio.sleep(1.0)
                rated = [
                    payload
                    for event, payload in again.events
                    if event == "update_message"
                    and isinstance(payload, dict)
                    and payload.get("id") == answer_id
                    and payload.get("feedback")
                ]

            assert rated, again.trace()
            assert rated[-1]["feedback"]["value"] == 1
        finally:
            await again.end_session()


class LlmRequests:
    """Запросы, которые приняла фейковая модель стенда."""

    def __init__(self, port: int) -> None:
        self._url = StandUrl.of(port, "/requests")

    def with_text(self, marker: str) -> dict[str, Any]:
        """Последний запрос, в сообщениях которого есть marker."""
        response = httpx.get(self._url, timeout=10.0)
        response.raise_for_status()
        found = [
            payload
            for payload in response.json()["requests"]
            if marker in json.dumps(payload.get("messages"))
        ]
        assert found, f"the fake llm got no request with {marker!r}"

        return found[-1]
