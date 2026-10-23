"""Ретранслятор ACP службы агента: подключение клиента и процессы агента
его сессий.

Подключение клиента (ClientLink) держит процессы агента: свободный — для
initialize, session/list, session/delete и прочих методов без сессии — и по
процессу на открытую сессию. session/new, session/resume и session/fork
забирают свободный процесс или запускают новый, session/close гасит процесс
сессии. Сообщения перекладываются сырыми кадрами JSON-RPC: у каждой стороны
свои id запросов, $/cancel_request переводится на id другой стороны,
уведомление mcp/message клиента уходит процессу, чей запрос mcp/message
идёт. Кадры одного направления одного процесса уходят в том порядке, в
каком пришли. Простой подключения и неожиданный выход процесса открытой
сессии закрывают подключение целиком.

Ошибки:
AgentLaunchError — процесс агента не запустился: предел процессов службы,
    каталог процесса не записан или исполняемый файл не стартовал.
Клиенту ошибки уходят ответами JSON-RPC с кодами ACP; ClientLink наружу
ничего не выпускает.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from itertools import count
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from acp._transport import Transport
from acp.exceptions import RequestError
from acp.experimental.negotiation import AgentProtocolConnection
from acp.experimental.v2.meta import AGENT_METHODS, CLIENT_METHODS, PROTOCOL_METHODS
from acp.transports import spawn_stdio_transport
from pydantic import BaseModel, ConfigDict, Field

from boba.access import RoleVisibility
from boba.agent.settings import (
    ModelSettings,
    PostgresHistorySettings,
    SettingsLayer,
)
from boba.core.secrets import SecretReveal
from boba.transport.acp.protocol import AcpErrorCode, AcpTransportError, BatchTransport

__all__ = [
    "AcpMethod",
    "AgentChild",
    "AgentLaunchError",
    "AgentLauncher",
    "AgentProcessSection",
    "AgentProfile",
    "AgentSettings",
    "Caller",
    "ClientLink",
    "Frame",
    "ProcessQuota",
]

logger = logging.getLogger(__name__)

RequestId = int | str | None


class AgentLaunchError(Exception):
    """Процесс агента для подключения не запущен."""


class AcpMethod(StrEnum):
    """Методы ACP v2, которые служба разбирает сама, а не перекладывает
    вслепую."""

    INITIALIZE = AGENT_METHODS["initialize"]
    AUTH_LOGIN = AGENT_METHODS["auth_login"]
    AUTH_LOGOUT = AGENT_METHODS["auth_logout"]
    SESSION_NEW = AGENT_METHODS["session_new"]
    SESSION_RESUME = AGENT_METHODS["session_resume"]
    SESSION_FORK = AGENT_METHODS["session_fork"]
    SESSION_CLOSE = AGENT_METHODS["session_close"]
    SESSION_DELETE = AGENT_METHODS["session_delete"]
    SESSION_UPDATE = CLIENT_METHODS["session_update"]
    MCP_MESSAGE = AGENT_METHODS["mcp_message"]
    CANCEL_REQUEST = PROTOCOL_METHODS["cancel_request"]


class RpcKey(StrEnum):
    """Ключи кадра JSON-RPC и параметров ACP, которые читает ретранслятор,
    и версия JSON-RPC в кадрах службы."""

    JSONRPC = "jsonrpc"
    ID = "id"
    METHOD = "method"
    PARAMS = "params"
    RESULT = "result"
    ERROR = "error"
    SESSION_ID = "sessionId"
    REQUEST_ID = "requestId"
    UPDATE = "update"
    SESSION_UPDATE = "sessionUpdate"
    STATE = "state"
    AUTH_METHODS = "authMethods"
    METHOD_ID = "methodId"
    VERSION = "2.0"


class SessionState(StrEnum):
    """Состояние сессии в уведомлении session/update (state_update)."""

    UPDATE = "state_update"
    IDLE = "idle"


@dataclass(frozen=True)
class Frame:
    """Кадр JSON-RPC как он пришёл от клиента или процесса: запрос (метод и
    id), уведомление (метод без id) или ответ (id без метода)."""

    raw: Mapping[str, Any]

    def method(self) -> str | None:
        value = self.raw.get(RpcKey.METHOD)
        if isinstance(value, str):
            return value

        return None

    def id(self) -> RequestId:
        return self.raw.get(RpcKey.ID)

    def is_request(self) -> bool:
        if self.method() is None:
            return False

        return RpcKey.ID in self.raw

    def is_response(self) -> bool:
        if self.method() is not None:
            return False

        return RpcKey.ID in self.raw

    def params(self) -> Mapping[str, Any]:
        value = self.raw.get(RpcKey.PARAMS)
        if isinstance(value, Mapping):
            return value

        return {}

    def result(self) -> Mapping[str, Any] | None:
        value = self.raw.get(RpcKey.RESULT)
        if isinstance(value, Mapping):
            return value

        return None

    def session_id(self) -> str | None:
        """sessionId параметров запроса или уведомления."""
        value = self.params().get(RpcKey.SESSION_ID)
        if isinstance(value, str):
            return value

        return None

    def text(self, key: RpcKey) -> str | None:
        """Строковый параметр key."""
        value = self.params().get(key)
        if isinstance(value, str):
            return value

        return None

    def with_id(self, request_id: RequestId) -> Frame:
        raw = dict(self.raw)
        raw[RpcKey.ID] = request_id

        return Frame(raw)

    def with_result(self, result: Mapping[str, Any]) -> Frame:
        raw = dict(self.raw)
        raw[RpcKey.RESULT] = dict(result)

        return Frame(raw)

    def state(self) -> tuple[str, str] | None:
        """Сессия и состояние уведомления session/update вида state_update."""
        session = self.session_id()
        if session is None:
            return None

        update = self.params().get(RpcKey.UPDATE)
        if not isinstance(update, Mapping):
            return None

        if update.get(RpcKey.SESSION_UPDATE) != SessionState.UPDATE:
            return None

        state = update.get(RpcKey.STATE)
        if not isinstance(state, str):
            return None

        return session, state


class Frames:
    """Сборка кадров, которые служба шлёт от себя; создаётся подключением."""

    def request(self, request_id: int, method: str, params: Mapping[str, Any]) -> Frame:
        return Frame(
            {
                RpcKey.JSONRPC: RpcKey.VERSION,
                RpcKey.ID: request_id,
                RpcKey.METHOD: method,
                RpcKey.PARAMS: dict(params),
            }
        )

    def result(self, request_id: RequestId, result: Mapping[str, Any]) -> Frame:
        return Frame(
            {
                RpcKey.JSONRPC: RpcKey.VERSION,
                RpcKey.ID: request_id,
                RpcKey.RESULT: dict(result),
            }
        )

    def error(self, request_id: RequestId, error: RequestError) -> Frame:
        return Frame(
            {
                RpcKey.JSONRPC: RpcKey.VERSION,
                RpcKey.ID: request_id,
                RpcKey.ERROR: error.to_error_obj(),
            }
        )

    def cancel(self, request_id: RequestId) -> Frame:
        return Frame(
            {
                RpcKey.JSONRPC: RpcKey.VERSION,
                RpcKey.METHOD: AcpMethod.CANCEL_REQUEST.value,
                RpcKey.PARAMS: {RpcKey.REQUEST_ID: request_id},
            }
        )


@dataclass(frozen=True)
class Caller:
    """Вошедший пользователь подключения: логин из токена и профили агента,
    выданные его ролям (области токена)."""

    login: str
    profiles: frozenset[str]


class AgentProfile(RoleVisibility):
    """Профиль агента [agent.profiles.<имя>]: роли, которым он выдан, и
    профиль модели агента `models.<имя>` его файла настроек."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: ModelSettings


class AgentProcessSection(BaseModel):
    """Секция [agent.process]: команда агента до `--settings`, корень
    приватных каталогов процессов, сроки и пределы."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = Field(min_length=1)
    args: tuple[str, ...]
    workdir: str = Field(min_length=1)
    stop_timeout_sec: float = Field(gt=0)
    max_processes: int = Field(gt=0)
    """Сколько процессов агента служба держит разом на всех подключениях."""
    idle_sec: float = Field(gt=0)
    """Через сколько секунд тишины подключение без запросов в работе и без
    идущих ходов закрывается вместе с процессами."""


class SettingsFlag(StrEnum):
    """Флаг команды агента и файлы его приватного каталога."""

    SETTINGS = "--settings"
    FILE = "settings.json"
    ENCODING = "utf-8"
    HOME = "HOME"


class PrivateMode(IntEnum):
    """Права приватного каталога процесса и его файла настроек."""

    DIR = 0o700
    FILE = 0o600


class AgentSettings:
    """Файл настроек процесса агента для вошедшего пользователя.

    Создаётся сборкой службы из базового слоя [agent.settings] и профилей
    [agent.profiles]; им пользуется AgentLauncher. Файл — базовый слой,
    профили моделей, выданные ролям вошедшего (`models`), первый из них по
    порядку конфига — профилем по умолчанию (`model`), и владелец истории
    (`history.owner`) — логин вошедшего: процесс видит только его сессии.
    Остальные профили выбираются в сессии настройкой ACP `model`.
    """

    def __init__(
        self, base: SettingsLayer, profiles: Mapping[str, AgentProfile]
    ) -> None:
        self._base = base
        self._profiles = dict(profiles)

    def granted(self, caller: Caller) -> list[str]:
        """Профили вошедшего в порядке конфига."""
        names: list[str] = []
        for name in self._profiles:
            if name not in caller.profiles:
                continue

            names.append(name)

        return names

    def document(self, caller: Caller) -> dict[str, object]:
        """Ошибки:
        AgentLaunchError — ролям вошедшего не выдан ни один профиль.
        """
        granted = self.granted(caller)
        if not granted:
            known = ", ".join(self._profiles)
            msg = (
                f"agent settings for {caller.login!r}: the token grants none of "
                f"the agent profiles {known}; got scopes {sorted(caller.profiles)}"
            )
            raise AgentLaunchError(msg)

        models: dict[str, Mapping[str, Any]] = {}
        for name in granted:
            models[name] = self._dumped(self._profiles[name].model)

        history = self._base.history
        update: dict[str, object] = {"model": granted[0], "models": models}
        if isinstance(history, PostgresHistorySettings):
            update["history"] = history.model_copy(update={"owner": caller.login})

        return self._dumped(self._base.model_copy(update=update))

    def _dumped(self, model: BaseModel) -> dict[str, Any]:
        """Дамп ключами файла с открытыми секретами."""
        dump = model.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
            context={SecretReveal.REVEAL_CONTEXT: True},
        )
        SecretReveal.apply(model, dump)

        return dump


class ProcessQuota:
    """Предел процессов агента службы на всех подключениях.

    Создаётся сборкой службы; запуск процесса (AgentLauncher) берёт место,
    остановка процесса его отдаёт.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._taken = 0

    def take(self, caller: Caller) -> None:
        """Ошибки:
        AgentLaunchError — все места заняты.
        """
        if self._taken >= self._limit:
            msg = (
                f"starting an agent process for {caller.login!r}: the service "
                f"already runs its limit of {self._limit} agent processes; close "
                "a session or retry later"
            )
            raise AgentLaunchError(msg)

        self._taken += 1

    def give(self) -> None:
        self._taken -= 1

    def taken(self) -> int:
        return self._taken


class AgentChild:
    """Запущенный процесс агента: кадры по его stdio, журнал stderr в лог
    службы и хвостом для текстов ошибок, остановка.

    Создаёт AgentLauncher; держит дорожка подключения (Lane). Остановка
    закрывает stdin, ждёт выхода и добивает процесс, удаляет приватный
    каталог и отдаёт место предела.
    """

    TAIL_LINES: ClassVar[int] = 20

    def __init__(
        self,
        transport: BatchTransport,
        stack: AsyncExitStack,
        label: str,
    ) -> None:
        self._transport = transport
        self._stack = stack
        self._label = label
        self._tail: deque[str] = deque(maxlen=self.TAIL_LINES)

    def label(self) -> str:
        return self._label

    async def send(self, frame: Frame) -> None:
        await self._transport.send(dict(frame.raw))

    async def receive(self) -> Frame | None:
        """Следующий кадр процесса; None — вывод кончился.

        Ошибки:
        AcpTransportError — строка вывода длиннее предела потока.
        """
        raw = await self._transport.receive()
        if raw is None:
            return None

        return Frame(raw)

    async def drain_log(self, stderr: asyncio.StreamReader) -> None:
        """Строки stderr процесса — в лог службы с меткой процесса."""
        while True:
            line = await stderr.readline()
            if not line:
                return

            text = line.decode(SettingsFlag.ENCODING, errors="replace").rstrip()
            self._tail.append(text)
            logger.info("%s: %s", self._label, text)

    def tail(self) -> str:
        return " | ".join(self._tail)

    async def stop(self) -> None:
        await self._stack.aclose()


class AgentLauncher:
    """Запуск процесса агента для вошедшего: место в пределе, приватный
    каталог с файлом настроек 0600, процесс по stdio.

    Создаётся сборкой службы; зовёт подключение (ClientLink) на каждую
    дорожку. Процесс получает минимальное окружение SDK и HOME в своём
    каталоге; всё остальное ему несёт файл настроек.
    """

    LINE_LIMIT: ClassVar[int] = 50 * 1024 * 1024

    def __init__(
        self, process: AgentProcessSection, settings: AgentSettings, quota: ProcessQuota
    ) -> None:
        self._process = process
        self._settings = settings
        self._quota = quota

    async def start(self, caller: Caller) -> AgentChild:
        """Ошибки:
        AgentLaunchError — предел процессов, профилей нет, каталог не
            записан или процесс не стартовал.
        """
        document = self._settings.document(caller)
        self._quota.take(caller)
        stack = AsyncExitStack()
        stack.callback(self._quota.give)
        try:
            return await self._started(caller, document, stack)
        except BaseException:
            await stack.aclose()
            raise

    async def _started(
        self, caller: Caller, document: Mapping[str, object], stack: AsyncExitStack
    ) -> AgentChild:
        workdir = Path(self._process.workdir) / uuid4().hex
        settings = workdir / SettingsFlag.FILE
        try:
            workdir.mkdir(mode=PrivateMode.DIR, parents=True)
            stack.callback(shutil.rmtree, workdir, True)
            descriptor = os.open(
                settings, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PrivateMode.FILE
            )
            with os.fdopen(descriptor, "w", encoding=SettingsFlag.ENCODING) as out:
                json.dump(document, out)
        except OSError as exc:
            msg = (
                f"starting an agent process for {caller.login!r}: writing its "
                f"settings into {workdir} failed: {exc}"
            )
            raise AgentLaunchError(msg) from exc

        command = self._process.command
        # журнал stderr дочитывается после выхода процесса: выход из стека обратный
        drains: list[asyncio.Future[None]] = []
        stack.push_async_callback(self._finished, drains)
        try:
            stdout, stdin, process = await stack.enter_async_context(
                spawn_stdio_transport(
                    command,
                    *self._process.args,
                    SettingsFlag.SETTINGS,
                    str(settings),
                    env={SettingsFlag.HOME: str(workdir)},
                    cwd=workdir,
                    limit=self.LINE_LIMIT,
                    shutdown_timeout=self._process.stop_timeout_sec,
                )
            )
        except OSError as exc:
            msg = (
                f"starting an agent process for {caller.login!r}: {command!r} "
                f"did not start in {workdir}: {exc}; check [agent.process].command"
            )
            raise AgentLaunchError(msg) from exc

        label = f"agent pid {process.pid} [user={caller.login}]"
        child = AgentChild(BatchTransport(stdout, stdin), stack, label)
        stderr = process.stderr
        if stderr is not None:
            drains.append(asyncio.ensure_future(child.drain_log(stderr)))

        logger.info("%s: started", label)

        return child

    @staticmethod
    async def _finished(drains: Sequence[asyncio.Future[None]]) -> None:
        for drain in drains:
            await drain


class Purpose(StrEnum):
    """Зачем запрос клиента ушёл процессу: от этого зависит, что служба
    делает с ответом."""

    FORWARD = "forward"
    INITIALIZE = "initialize"
    REPLAY = "replay"
    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True)
class ClientCall:
    """Запрос клиента в работе у процесса: его id у клиента, метод, цель и
    сессия, которую открывает session/resume (у new и fork её даёт ответ)."""

    client_id: RequestId
    method: str
    purpose: Purpose
    session_id: str | None


@dataclass(frozen=True)
class AgentCall:
    """Запрос процесса в работе у клиента: дорожка, id у процесса и
    requestId, если это mcp/message."""

    lane: Lane
    agent_id: RequestId
    mcp_request: str | None


@dataclass(eq=False)
class Lane:
    """Дорожка подключения: процесс агента, очередь кадров к нему и запросы
    клиента у него в работе. Сессия — открытая в процессе; opening —
    сколько запросов открытия идёт."""

    key: int
    queue: asyncio.Queue[Frame] = field(default_factory=asyncio.Queue)
    calls: dict[RequestId, ClientCall] = field(default_factory=dict)
    ids: Iterator[int] = field(default_factory=count)
    session: str | None = None
    opening: int = 0
    closing: bool = False
    child: AgentChild | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def label(self) -> str:
        if self.child is None:
            return f"agent lane {self.key} (not started)"

        return self.child.label()


Closer = Callable[[], Awaitable[None]]


class ClientLink(AgentProtocolConnection):
    """Подключение клиента ACP к службе: кадры клиента — процессам агента его
    сессий, кадры процессов — клиенту.

    Реализация соединения протокола SDK (AgentProtocolConnection): его
    создаёт роутер службы на каждое подключение streamable HTTP или
    WebSocket с уже проверенным входом. listen() читает кадры клиента до
    конца потока, close() гасит процессы и закрывает транспорт; закрыться
    подключение может и само — по простою, по auth/logout и при выходе
    процесса открытой сессии, тогда closed сообщает об этом транспорту.
    """

    TICK_SEC: ClassVar[float] = 1.0

    def __init__(  # noqa: PLR0913 — подключение собирается всеми своими входами
        self,
        transport: Transport,
        caller: Caller,
        launcher: AgentLauncher,
        auth_methods: Sequence[Mapping[str, Any]],
        idle_sec: float,
        listening: bool,
    ) -> None:
        self._transport = transport
        self._caller = caller
        self._launcher = launcher
        self._auth_methods = list(auth_methods)
        self._idle_sec = idle_sec
        self._frames = Frames()
        self._send_lock = asyncio.Lock()
        self._lanes: dict[int, Lane] = {}
        self._lane_keys = count()
        self._free: Lane | None = None
        self._sessions: dict[str, Lane] = {}
        self._by_client: dict[RequestId, tuple[Lane, RequestId]] = {}
        self._agent_calls: dict[int, AgentCall] = {}
        self._net_ids = count()
        self._mcp_requests: dict[str, Lane] = {}
        self._busy: set[str] = set()
        self._initialize: Mapping[str, Any] | None = None
        self._last = time.monotonic()
        self._closing = False
        self._closed: list[Closer] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._watch = asyncio.ensure_future(self._watch_idle())
        self._reader: asyncio.Task[None] | None = None
        if listening:
            self._reader = asyncio.ensure_future(self._read_client())

    def caller(self) -> Caller:
        return self._caller

    def on_closed(self, closer: Closer) -> None:
        """Что сделать, когда подключение закрылось само или по запросу."""
        self._closed.append(closer)

    async def listen(self) -> None:
        await self._read_client()

    async def close(self) -> None:
        if self._closing:
            return

        self._closing = True
        self._watch.cancel()
        current = asyncio.current_task()
        if self._reader is not None and self._reader is not current:
            self._reader.cancel()

        lanes = list(self._lanes.values())
        self._lanes.clear()
        self._sessions.clear()
        self._free = None
        stops: list[Awaitable[None]] = []
        for lane in lanes:
            stops.append(self._stop_lane(lane))

        await asyncio.gather(*stops)

        await self._transport.close()
        for closer in self._closed:
            await closer()

        logger.info(
            "connection of %r closed, %d agent process(es) stopped",
            self._caller.login,
            len(lanes),
        )

    async def _read_client(self) -> None:
        try:
            while True:
                raw = await self._transport.receive()
                if raw is None:
                    break

                self._last = time.monotonic()
                await self._on_client(Frame(raw))
        finally:
            self._spawn(self.close())

    def _spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, frame: Frame) -> None:
        """Кадр клиенту; оборванный транспорт закрывает подключение."""
        async with self._send_lock:
            try:
                await self._transport.send(dict(frame.raw))
            except ConnectionError as exc:
                logger.info(
                    "connection of %r: sending to the client failed: %s",
                    self._caller.login,
                    exc,
                )
                self._spawn(self.close())

    async def _refuse(self, frame: Frame, error: RequestError) -> None:
        if not frame.is_request():
            logger.info(
                "connection of %r: dropped %s: %s",
                self._caller.login,
                frame.method(),
                error.data,
            )
            return

        await self._send(self._frames.error(frame.id(), error))

    # -- кадры клиента ------------------------------------------------------

    async def _on_client(self, frame: Frame) -> None:
        if frame.is_response():
            self._answer_agent(frame)
            return

        method = frame.method()
        if method is None:
            logger.info(
                "connection of %r: dropped a frame that is neither a request, "
                "a notification nor a response: %s",
                self._caller.login,
                json.dumps(frame.raw)[:200],
            )
            return

        if method == AcpMethod.INITIALIZE:
            await self._initialize_link(frame)
            return

        if self._initialize is None:
            details = (
                f"{method}: the connection is not initialized; send initialize first"
            )
            await self._refuse(
                frame, RequestError.invalid_request({"details": details})
            )
            return

        if not frame.is_request():
            self._notify_agent(frame, method)
            return

        await self._route_request(frame, method)

    async def _initialize_link(self, frame: Frame) -> None:
        if self._initialize is not None:
            details = "initialize: the connection is already initialized"
            await self._refuse(
                frame, RequestError.invalid_request({"details": details})
            )
            return

        self._initialize = frame.params()
        lane = self._new_lane(replay=False)
        self._free = lane
        self._enqueue(lane, frame, Purpose.INITIALIZE, None)

    async def _route_request(self, frame: Frame, method: str) -> None:
        session = frame.session_id()
        match method:
            case AcpMethod.AUTH_LOGIN:
                await self._login(frame)
            case AcpMethod.AUTH_LOGOUT:
                await self._send(self._frames.result(frame.id(), {}))
                self._spawn(self.close())
            case AcpMethod.SESSION_DELETE:
                await self._to_free(frame, Purpose.FORWARD, None)
            case AcpMethod.SESSION_CLOSE:
                await self._to_session(frame, Purpose.CLOSE)
            case AcpMethod.SESSION_RESUME if session in self._sessions:
                await self._to_session(frame, Purpose.FORWARD)
            case AcpMethod.SESSION_RESUME:
                await self._to_free(frame, Purpose.OPEN, session)
            case AcpMethod.SESSION_NEW | AcpMethod.SESSION_FORK:
                await self._to_free(frame, Purpose.OPEN, None)
            case _ if session is not None:
                await self._to_session(frame, Purpose.FORWARD)
            case _:
                await self._to_free(frame, Purpose.FORWARD, None)

    async def _login(self, frame: Frame) -> None:
        """auth/login: вход проверен транспортом до подключения, метод из
        initialize принимается сразу."""
        wanted = frame.text(RpcKey.METHOD_ID)
        known: list[str] = []
        for method in self._auth_methods:
            known.append(str(method.get(RpcKey.METHOD_ID)))

        if wanted not in known:
            details = (
                f"auth/login: methodId {wanted!r} is not an authentication method "
                f"of this service; known: {known}"
            )
            await self._refuse(frame, RequestError.invalid_params({"details": details}))
            return

        await self._send(self._frames.result(frame.id(), {}))

    async def _to_session(self, frame: Frame, purpose: Purpose) -> None:
        session = frame.session_id()
        lane = None
        if session is not None:
            lane = self._sessions.get(session)

        if lane is None:
            details = (
                f"{frame.method()}: session {session!r} is not open on this "
                "connection; open it with session/new, session/resume or session/fork"
            )
            error = RequestError(
                AcpErrorCode.RESOURCE_NOT_FOUND,
                "Resource not found",
                {"details": details},
            )
            await self._refuse(frame, error)
            return

        self._enqueue(lane, frame, purpose, session)

    async def _to_free(
        self, frame: Frame, purpose: Purpose, session: str | None
    ) -> None:
        """Свободному процессу подключения; открытие забирает его себе, а
        следующему свободному процессу запуститься придётся заново."""
        lane = self._free
        if lane is None:
            lane = self._new_lane(replay=True)
            self._free = lane

        if purpose is Purpose.OPEN:
            self._free = None
            lane.opening += 1

        self._enqueue(lane, frame, purpose, session)

    def _notify_agent(self, frame: Frame, method: str) -> None:
        """Уведомление клиента — процессу, к которому оно относится."""
        lane = self._notified_lane(frame, method)
        if lane is None:
            logger.info(
                "connection of %r: dropped notification %s with no agent process "
                "to receive it: %s",
                self._caller.login,
                method,
                json.dumps(frame.params())[:200],
            )
            return

        if method == AcpMethod.CANCEL_REQUEST:
            held = self._by_client.get(frame.params().get(RpcKey.REQUEST_ID))
            if held is None:
                return

            lane.queue.put_nowait(self._frames.cancel(held[1]))
            return

        lane.queue.put_nowait(frame)

    def _notified_lane(self, frame: Frame, method: str) -> Lane | None:
        if method == AcpMethod.CANCEL_REQUEST:
            held = self._by_client.get(frame.params().get(RpcKey.REQUEST_ID))
            if held is None:
                return None

            return held[0]

        if method == AcpMethod.MCP_MESSAGE:
            request = frame.text(RpcKey.REQUEST_ID)
            if request is None:
                return None

            return self._mcp_requests.get(request)

        session = frame.session_id()
        if session is not None:
            return self._sessions.get(session)

        return self._free

    def _answer_agent(self, frame: Frame) -> None:
        """Ответ клиента на запрос процесса — тому процессу с его id."""
        net_id = frame.id()
        if not isinstance(net_id, int):
            logger.info(
                "connection of %r: dropped a response to unknown request %r",
                self._caller.login,
                net_id,
            )
            return

        call = self._agent_calls.pop(net_id, None)
        if call is None:
            return

        if call.mcp_request is not None:
            self._mcp_requests.pop(call.mcp_request, None)

        if call.lane.closing:
            return

        call.lane.queue.put_nowait(frame.with_id(call.agent_id))

    # -- дорожки -------------------------------------------------------------

    def _new_lane(self, replay: bool) -> Lane:
        lane = Lane(key=next(self._lane_keys))
        self._lanes[lane.key] = lane
        if replay and self._initialize is not None:
            agent_id = next(lane.ids)
            lane.calls[agent_id] = ClientCall(
                client_id=None,
                method=AcpMethod.INITIALIZE.value,
                purpose=Purpose.REPLAY,
                session_id=None,
            )
            lane.queue.put_nowait(
                self._frames.request(agent_id, AcpMethod.INITIALIZE, self._initialize)
            )

        lane.tasks.append(asyncio.ensure_future(self._run_lane(lane)))

        return lane

    def _enqueue(
        self, lane: Lane, frame: Frame, purpose: Purpose, session: str | None
    ) -> None:
        agent_id = next(lane.ids)
        method = frame.method()
        if method is None:
            method = ""

        lane.calls[agent_id] = ClientCall(
            client_id=frame.id(), method=method, purpose=purpose, session_id=session
        )
        self._by_client[frame.id()] = (lane, agent_id)
        lane.queue.put_nowait(frame.with_id(agent_id))

    async def _run_lane(self, lane: Lane) -> None:
        """Запуск процесса дорожки и её очередь кадров к нему."""
        try:
            child = await self._launcher.start(self._caller)
        except AgentLaunchError as exc:
            logger.warning("connection of %r: %s", self._caller.login, exc)
            await self._lane_ended(lane, str(exc))
            return

        lane.child = child
        lane.tasks.append(asyncio.ensure_future(self._read_lane(lane, child)))
        while True:
            frame = await lane.queue.get()
            try:
                await child.send(frame)
            except (ConnectionError, OSError) as exc:
                logger.info("%s: writing to the process failed: %s", child.label(), exc)
                return

    async def _read_lane(self, lane: Lane, child: AgentChild) -> None:
        reason = f"{child.label()} closed its output"
        try:
            while True:
                frame = await child.receive()
                if frame is None:
                    break

                self._last = time.monotonic()
                await self._on_agent(lane, frame)
        except AcpTransportError as exc:
            reason = f"{child.label()}: {exc}"

        if lane.closing:
            return

        await self._lane_ended(lane, f"{reason}: {child.tail()}")

    async def _lane_ended(self, lane: Lane, reason: str) -> None:
        """Дорожка кончилась сама: запуск не удался или процесс вышел.
        Запросы клиента у неё получают отказ с причиной; процесс открытой
        сессии закрывает подключение целиком."""
        lane.closing = True
        self._lanes.pop(lane.key, None)
        if self._free is lane:
            self._free = None

        for call in list(lane.calls.values()):
            self._by_client.pop(call.client_id, None)
            if call.purpose is Purpose.REPLAY:
                continue

            details = f"{call.method}: the agent process is gone: {reason}"
            await self._send(
                self._frames.error(
                    call.client_id, RequestError.internal_error({"details": details})
                )
            )

        lane.calls.clear()
        self._forget_agent_calls(lane)
        self._spawn(self._stop_lane(lane))
        if lane.session is None:
            return

        logger.warning(
            "connection of %r: the agent process of session %s ended (%s); "
            "closing the connection",
            self._caller.login,
            lane.session,
            reason,
        )
        self._spawn(self.close())

    def _forget_agent_calls(self, lane: Lane) -> None:
        for net_id, call in list(self._agent_calls.items()):
            if call.lane is not lane:
                continue

            del self._agent_calls[net_id]
            if call.mcp_request is not None:
                self._mcp_requests.pop(call.mcp_request, None)

    async def _stop_lane(self, lane: Lane) -> None:
        lane.closing = True
        current = asyncio.current_task()
        for task in lane.tasks:
            if task is current:
                continue

            task.cancel()

        child = lane.child
        if child is None:
            return

        lane.child = None
        if lane.session is not None:
            self._busy.discard(lane.session)

        await child.stop()
        logger.info("%s: stopped", child.label())

    # -- кадры процессов -----------------------------------------------------

    async def _on_agent(self, lane: Lane, frame: Frame) -> None:
        if frame.is_response():
            await self._agent_answered(lane, frame)
            return

        method = frame.method()
        if method is None:
            logger.info(
                "%s: dropped a frame that is neither a request, a notification "
                "nor a response",
                lane.label(),
            )
            return

        if frame.is_request():
            net_id = next(self._net_ids)
            mcp_request = None
            if method == AcpMethod.MCP_MESSAGE:
                mcp_request = frame.text(RpcKey.REQUEST_ID)

            if mcp_request is not None:
                self._mcp_requests[mcp_request] = lane

            self._agent_calls[net_id] = AgentCall(
                lane=lane, agent_id=frame.id(), mcp_request=mcp_request
            )
            await self._send(frame.with_id(net_id))
            return

        if method == AcpMethod.CANCEL_REQUEST:
            await self._agent_cancelled(lane, frame)
            return

        self._track_state(frame)
        await self._send(frame)

    async def _agent_cancelled(self, lane: Lane, frame: Frame) -> None:
        """$/cancel_request процесса — клиенту с id запроса у клиента."""
        wanted = frame.params().get(RpcKey.REQUEST_ID)
        for net_id, call in self._agent_calls.items():
            if call.lane is not lane:
                continue

            if call.agent_id != wanted:
                continue

            await self._send(self._frames.cancel(net_id))
            return

    def _track_state(self, frame: Frame) -> None:
        """Идущие ходы: подключение с ними не простаивает."""
        state = frame.state()
        if state is None:
            return

        session, value = state
        if value == SessionState.IDLE:
            self._busy.discard(session)
            return

        self._busy.add(session)

    async def _agent_answered(self, lane: Lane, frame: Frame) -> None:
        call = lane.calls.pop(frame.id(), None)
        if call is None:
            logger.info(
                "%s: dropped a response to unknown request %r", lane.label(), frame.id()
            )
            return

        self._by_client.pop(call.client_id, None)
        match call.purpose:
            case Purpose.REPLAY:
                self._replayed(lane, frame)
                return
            case Purpose.INITIALIZE:
                await self._send(self._initialized(frame).with_id(call.client_id))
                return
            case Purpose.OPEN:
                self._opened(lane, frame, call)
            case Purpose.CLOSE:
                self._closed_session(lane, frame, call)
            case Purpose.FORWARD:
                pass

        await self._send(frame.with_id(call.client_id))

    def _replayed(self, lane: Lane, frame: Frame) -> None:
        """Ответ процесса на initialize, который служба повторила за клиентом."""
        if frame.result() is not None:
            return

        logger.warning(
            "%s: refused the initialize of the connection: %s",
            lane.label(),
            json.dumps(frame.raw.get(RpcKey.ERROR))[:300],
        )

    def _initialized(self, frame: Frame) -> Frame:
        """Ответ initialize клиенту: способы входа — службы, а не процесса."""
        result = frame.result()
        if result is None:
            return frame

        updated = dict(result)
        updated[RpcKey.AUTH_METHODS] = self._auth_methods

        return frame.with_result(updated)

    def _opened(self, lane: Lane, frame: Frame, call: ClientCall) -> None:
        """Ответ на открытие: удачное привязывает сессию к процессу, неудачное
        возвращает процесс в свободные или гасит его."""
        lane.opening -= 1
        session = call.session_id
        result = frame.result()
        if result is not None:
            given = result.get(RpcKey.SESSION_ID)
            if isinstance(given, str):
                session = given

        if result is not None and session is not None and lane.session is None:
            lane.session = session
            self._sessions[session] = lane
            logger.info("%s: holds session %s (%s)", lane.label(), session, call.method)
            return

        if lane.session is not None:
            return

        if lane.opening > 0:
            return

        if self._free is None:
            self._free = lane
            return

        self._lanes.pop(lane.key, None)
        self._spawn(self._stop_lane(lane))

    def _closed_session(self, lane: Lane, frame: Frame, call: ClientCall) -> None:
        """session/close удался: процесс сессии больше не нужен."""
        if frame.result() is None:
            return

        session = call.session_id
        if session is not None:
            self._sessions.pop(session, None)

        self._lanes.pop(lane.key, None)
        self._spawn(self._stop_lane(lane))

    # -- простой -------------------------------------------------------------

    def idle(self) -> bool:
        """Подключение простаивает: запросов в работе и идущих ходов нет, и
        тишина дольше idle_sec."""
        if self._by_client:
            return False

        if self._agent_calls:
            return False

        if self._busy:
            return False

        return time.monotonic() - self._last >= self._idle_sec

    async def _watch_idle(self) -> None:
        while True:
            await asyncio.sleep(self.TICK_SEC)
            if not self.idle():
                continue

            logger.info(
                "connection of %r is idle for %.0fs: closing it",
                self._caller.login,
                self._idle_sec,
            )
            self._spawn(self.close())
            return
