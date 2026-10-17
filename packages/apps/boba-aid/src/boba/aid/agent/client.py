"""ACP-клиент чата aid на v2: процесс агента, соединение ClientConnection по
его stdio, методы сессии; уведомления и вопросы агента уходят приёмнику с
явным интерфейсом SessionUpdates, который заменяет рендерер ленты, а запросы
mcp/message — ретранслятору (McpRelay).

Ошибки:
AgentProcessError — процесс не запустился, не прошёл рукопожатие (чужая
    версия ACP, нет серверов type "acp"), умер или соединение с ним закрыто.
AgentRequestError — агент ответил ошибкой JSON-RPC: метод, код, текст, data.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from asyncio.subprocess import Process
from collections.abc import Awaitable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeAlias, TypeVar
from uuid import UUID

from acp.exceptions import RequestError
from acp.experimental.v2 import Client, schema
from acp.transports import spawn_stdio_transport
from pydantic import BaseModel, ValidationError

from boba.aid.agent.relay import ClientName, McpRelay
from boba.aid.agent.settings import AgentWorkdir, WorkdirFile
from boba.aid.rendering.acp_view import SessionUpdate
from boba.transport.acp.protocol import (
    AcpVersion,
    ClientConnection,
    ErrorDataKey,
    McpMessage,
    McpOutcome,
    MetaKey,
)

__all__ = [
    "AcpClientError",
    "AgentClient",
    "AgentCommand",
    "AgentProcess",
    "AgentProcessError",
    "AgentRequestError",
    "PromptBlock",
    "SessionUpdates",
]

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

PromptBlock: TypeAlias = schema.TextContentBlock | schema.ResourceContentBlock


class AcpClientError(Exception):
    """Основа ошибок клиента ACP."""


class AgentProcessError(AcpClientError):
    """Процесс агента не запустился, не ответил или умер."""


class AgentRequestError(AcpClientError):
    """Агент ответил ошибкой JSON-RPC; текст — метод, код, сообщение и
    подробности из `data.details`, когда агент их прислал."""

    def __init__(self, method: str, code: int, message: str, data: Any) -> None:
        super().__init__(self._text(method, code, message, data))
        self.method = method
        self.code = code
        self.data = data

    @staticmethod
    def _text(method: str, code: int, message: str, data: Any) -> str:
        text = f"{method}: agent answered error {code}: {message}"
        if not isinstance(data, Mapping):
            return text

        details = data.get(ErrorDataKey.DETAILS.value)
        if not details:
            return text

        return f"{text}: {details}"


class SessionUpdates(Protocol):
    """Приёмник потока сессии: уведомления, вопросы разрешений и итог хода.
    Реализуют приёмник ленты (AgentFeed), приёмник без ленты (MuteUpdates)
    и обёртка агента треда (AskedQuestions)."""

    @abstractmethod
    async def update(self, session_id: str, update: SessionUpdate) -> None:
        """Уведомление `session/update` живого хода или воспроизведения."""

    @abstractmethod
    async def permission(
        self, session_id: str, title: str, options: Sequence[schema.PermissionOption]
    ) -> schema.RequestPermissionResponse:
        """Вопрос `session/request_permission`: ответ выбирает приёмник."""

    @abstractmethod
    async def turn_finished(
        self, session_id: str, idle: schema.IdleSessionStateUpdate
    ) -> None:
        """Агент сообщил idle: ход закончен с причиной stopReason."""

    @abstractmethod
    async def abandon(self, session_id: str) -> None:
        """Ход сорвался без idle: открытые узлы ленты закрываются."""


class AgentClient(Client):
    """Реализация Client SDK v2 для чата aid: уведомления и вопросы
    разрешений — приёмнику; остальные методы протокола клиент не обслуживает,
    и SDK сам отвечает на них method_not_found."""

    def __init__(self, updates: SessionUpdates) -> None:
        self._updates = updates

    async def request_permission(
        self,
        session_id: str,
        title: str,
        options: list[schema.PermissionOption],
        description: str | None = None,
        subject: schema.ToolCallPermissionSubjectVariant
        | schema.CommandPermissionSubjectVariant
        | schema.OtherPermissionSubject
        | None = None,
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        return await self._updates.permission(session_id, title, options)

    async def session_update(
        self, session_id: str, update: SessionUpdate, **kwargs: Any
    ) -> None:
        await self._updates.update(session_id, update)

    # SDK считает метод протокола, оставленный как есть, неподдержанным
    connect_mcp = Client.connect_mcp
    mcp_message = Client.mcp_message
    notify_mcp = Client.notify_mcp
    disconnect_mcp = Client.disconnect_mcp
    create_elicitation = Client.create_elicitation
    complete_elicitation = Client.complete_elicitation


@dataclass(frozen=True)
class AgentCommand:
    """Команда запуска агента: исполняемый файл, аргументы до `--settings`,
    нечувствительные переменные окружения поверх минимального набора SDK."""

    executable: str
    args: Sequence[str]
    env: Mapping[str, str]
    start_timeout_sec: float
    stop_timeout_sec: float

    SETTINGS_FLAG: ClassVar[str] = "--settings"

    def argv(self, settings_path: Path) -> list[str]:
        return [*self.args, self.SETTINGS_FLAG, str(settings_path)]

    def with_env(self, extra: Mapping[str, str]) -> AgentCommand:
        """Команда с добавленными переменными запуска (пути libkrb5 истории)."""
        merged = dict(self.env)
        merged.update(extra)

        return AgentCommand(
            executable=self.executable,
            args=self.args,
            env=merged,
            start_timeout_sec=self.start_timeout_sec,
            stop_timeout_sec=self.stop_timeout_sec,
        )


class AgentProcess:
    """Процесс агента ACP v2 одной вкладки: запуск из приватного каталога,
    соединение ClientConnection по его stdio, методы сессии, остановка и
    удаление каталога.

    Создаётся запуском агента (AgentLauncher) с объявлениями серверов
    процесса: их получает каждая сессия процесса. Процессом управляет
    spawn_stdio_transport SDK; stderr агента пишется в журнал приватного
    каталога и читается при отказе. Запросы mcp/message агента отдаются
    ретранслятору вместе с отправкой уведомлений этого соединения.
    """

    LINE_LIMIT: ClassVar[int] = 16 * 1024 * 1024
    LOG_TAIL_LINES: ClassVar[int] = 20

    def __init__(  # noqa: PLR0913 — процесс собирается всеми своими входами
        self,
        command: AgentCommand,
        workdir: AgentWorkdir,
        client: Client,
        relay: McpRelay,
        servers: Sequence[schema.AcpMcpServer],
        version: str,
    ) -> None:
        self._command = command
        self._workdir = workdir
        self._client = client
        self._relay = relay
        self._servers = list(servers)
        self._version = version
        self._stack = AsyncExitStack()
        self._process: Process | None = None
        self._connection: ClientConnection | None = None
        self._forks_by_message = False
        """Агент объявил срез ветки session/fork по messageId вопроса."""

    @property
    def workdir(self) -> AgentWorkdir:
        return self._workdir

    @property
    def pid(self) -> int:
        return self._require_process().pid

    def alive(self) -> bool:
        if self._process is None:
            return False

        return self._process.returncode is None

    async def exited(self) -> int:
        """Код выхода процесса, когда он завершится."""
        return await self._require_process().wait()

    async def start(self) -> schema.InitializeResponse:
        """Запуск и рукопожатие v2; процесс, не ответивший в срок, говорящий
        на чужой версии или без серверов type "acp", гасится.

        Ошибки:
        AgentProcessError — запуск или initialize не состоялись.
        """
        executable = self._command.executable
        stderr_path = self._workdir.stderr_path
        try:
            stderr = stderr_path.open("ab")
        except OSError as exc:
            msg = (
                f"agent process {executable!r}: opening its stderr log "
                f"{stderr_path} failed: {exc}"
            )
            raise AgentProcessError(msg) from exc

        with stderr:
            try:
                stdout, stdin, process = await self._stack.enter_async_context(
                    spawn_stdio_transport(
                        executable,
                        *self._command.argv(self._workdir.settings_path),
                        env=self._command.env,
                        cwd=self._workdir.path,
                        stderr=stderr.fileno(),
                        limit=self.LINE_LIMIT,
                        shutdown_timeout=self._command.stop_timeout_sec,
                    )
                )
            except OSError as exc:
                msg = f"agent process {executable!r} did not start: {exc}"
                raise AgentProcessError(msg) from exc

        self._process = process
        self._connection = ClientConnection(self._client, self._answer, stdin, stdout)
        # соединение закрывается раньше процесса: выход из стека — в обратном порядке
        self._stack.push_async_callback(self._connection.close)
        try:
            response = await asyncio.wait_for(
                self._initialize(), self._command.start_timeout_sec
            )
        except TimeoutError as exc:
            msg = (
                f"agent process {executable!r} (pid {process.pid}) did not answer "
                f"initialize within {self._command.start_timeout_sec}s: "
                f"{self.log_tail()}"
            )
            await self.close()
            raise AgentProcessError(msg) from exc
        except AcpClientError as exc:
            msg = (
                f"agent process {executable!r} failed the ACP handshake, this "
                f"chat speaks ACP version {AcpVersion.V2.value}: {exc}: "
                f"{self.log_tail()}"
            )
            await self.close()
            raise AgentProcessError(msg) from exc

        session = None
        if response.capabilities is not None:
            session = response.capabilities.session

        mcp = None
        if session is not None:
            mcp = session.mcp

        self._forks_by_message = self._cuts_forks(session)

        acp = None
        if mcp is not None:
            acp = mcp.acp

        if acp is None:
            msg = (
                f"agent process {executable!r} (pid {process.pid}): agent "
                f"{response.info.name!r} does not declare "
                "capabilities.session.mcp.acp in initialize, expected it to take "
                "the MCP servers of the chat as servers of type 'acp'"
            )
            await self.close()
            raise AgentProcessError(msg)

        logger.info(
            "agent process started: pid=%s agent=%s/%s",
            process.pid,
            response.info.name,
            response.info.version,
        )

        return response

    async def open_session(self) -> schema.NewSessionResponse:
        """Новая сессия в приватном каталоге с серверами процесса; id и
        настройки (`configOptions`) выдаёт агент."""
        connection = self._require_connection()

        return await self._request(
            "session/new",
            connection.new_session(
                cwd=str(self._workdir.path), mcp_servers=list(self._servers)
            ),
        )

    async def resume_session(
        self, session_id: str, replay: bool
    ) -> schema.ResumeSessionResponse:
        """Открытие сохранённой сессии; replay — беседа воспроизводится
        приёмнику с начала до ответа, иначе прошлых ходов он не получает."""
        connection = self._require_connection()
        replay_from: schema.ReplayFromStartVariant | None = None
        if replay:
            replay_from = schema.ReplayFromStartVariant()

        return await self._request(
            "session/resume",
            connection.resume_session(
                session_id=session_id,
                cwd=str(self._workdir.path),
                mcp_servers=list(self._servers),
                replay_from=replay_from,
            ),
        )

    async def prompt(self, session_id: str, blocks: Sequence[PromptBlock]) -> str:
        """Вопрос в сессию; ответ — messageId, который агент дал вопросу. Он
        же uuid записи вопроса у агента и id шага вопроса в чате.

        Ошибки:
        AgentProcessError — агент дал вопросу id, который не uuid.
        """
        connection = self._require_connection()
        response = await self._request(
            "session/prompt",
            connection.prompt(session_id=session_id, prompt=list(blocks)),
        )
        try:
            UUID(response.message_id)
        except ValueError as exc:
            msg = (
                f"session/prompt of session {session_id}: the agent named the "
                f"question {response.message_id!r}, expected a uuid that becomes "
                "the id of the question step"
            )
            raise AgentProcessError(msg) from exc

        return response.message_id

    async def cancel(self, session_id: str) -> None:
        connection = self._require_connection()
        try:
            await connection.cancel_session(session_id=session_id)
        except ConnectionError as exc:
            msg = f"session/cancel: the agent connection is closed: {exc}"
            raise AgentProcessError(msg) from exc

    async def close_session(self, session_id: str) -> None:
        connection = self._require_connection()
        await self._request(
            "session/close", connection.close_session(session_id=session_id)
        )

    async def delete_session(self, session_id: str) -> None:
        connection = self._require_connection()
        await self._request(
            "session/delete", connection.delete_session(session_id=session_id)
        )

    @staticmethod
    def _cuts_forks(session: schema.SessionCapabilities | None) -> bool:
        """Объявил ли агент в `capabilities.session.fork._meta` срез ветки
        session/fork по messageId вопроса."""
        fork = None
        if session is not None:
            fork = session.fork

        cut = None
        if fork is not None:
            cut = fork.field_meta

        declared = None
        if cut is not None:
            declared = cut.get(MetaKey.UP_TO_MESSAGE_ID.value)

        return declared is True

    def forks_by_message(self) -> bool:
        """Агент объявил в `capabilities.session.fork._meta`, что режет ветку
        session/fork по messageId вопроса: правку вопроса можно вести веткой."""
        return self._forks_by_message

    async def fork(
        self, session_id: str, up_to_message_id: str
    ) -> schema.ForkSessionResponse:
        """Ветвь сессии до вопроса up_to_message_id (сам вопрос и всё после
        него в ветку не идут); id ветки и её настройки выдаёт агент."""
        connection = self._require_connection()
        # SDK кладёт лишние именованные аргументы метода в _meta запроса
        meta: dict[str, Any] = {MetaKey.UP_TO_MESSAGE_ID.value: up_to_message_id}

        return await self._request(
            "session/fork",
            connection.fork_session(
                session_id=session_id,
                cwd=str(self._workdir.path),
                mcp_servers=list(self._servers),
                **meta,
            ),
        )

    async def close(self) -> None:
        """Соединение закрывается, процесс гасит SDK (конец ввода, ожидание,
        terminate и kill); каталог с файлом настроек удаляется в любом исходе."""
        try:
            await self._stack.aclose()
        finally:
            self._workdir.remove()

        if self._process is not None:
            logger.info(
                "agent process exited: pid=%s code=%s",
                self._process.pid,
                self._process.returncode,
            )

    def log_tail(self) -> str:
        """Хвост stderr агента: причина отказа без него не видна."""
        path = self._workdir.stderr_path
        if not path.is_file():
            return ""

        text = path.read_text(encoding=WorkdirFile.ENCODING.value, errors="replace")

        return "\n".join(text.splitlines()[-self.LOG_TAIL_LINES :])

    async def _answer(self, message: McpMessage) -> McpOutcome:
        return await self._relay.answer(message, self._require_connection().notify)

    async def _initialize(self) -> schema.InitializeResponse:
        """Рукопожатие v2: версию ответа проверяет SDK.

        Ошибки:
        AgentRequestError — агент отказал либо ответил чужой версией.
        AgentProcessError — соединение закрыто или ответ не по схеме.
        """
        connection = self._require_connection()
        info = schema.Implementation(name=ClientName.NAME.value, version=self._version)

        return await self._request(
            "initialize",
            connection.initialize(
                protocol_version=AcpVersion.V2.value,
                info=info,
                capabilities=schema.ClientCapabilities(),
            ),
        )

    async def _request(self, method: str, call: Awaitable[ModelT]) -> ModelT:
        """Ответ агента на метод method.

        Ошибки:
        AgentRequestError — ответ-ошибка JSON-RPC.
        AgentProcessError — соединение закрыто или ответ не по схеме.
        """
        try:
            return await call
        except RequestError as exc:
            raise AgentRequestError(method, exc.code, str(exc), exc.data) from exc
        except ConnectionError as exc:
            msg = f"{method}: the agent connection is closed: {exc}: {self.log_tail()}"
            raise AgentProcessError(msg) from exc
        except ValidationError as exc:
            msg = f"{method}: the agent answered off the ACP v2 schema: {exc}"
            raise AgentProcessError(msg) from exc

    def _require_process(self) -> Process:
        if self._process is None:
            raise AgentProcessError(self._unstarted("its pid and exit"))

        return self._process

    def _require_connection(self) -> ClientConnection:
        if self._connection is None:
            raise AgentProcessError(self._unstarted("an ACP request"))

        return self._connection

    def _unstarted(self, wanted: str) -> str:
        """Текст отказа процессу, к которому обратились до запуска."""
        return (
            f"agent process {self._command.executable!r} in {self._workdir.path}: "
            f"{wanted} needs a started process, got none: start() was not called"
        )
