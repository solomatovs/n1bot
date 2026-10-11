"""ACP-клиент chainlit: процесс агента, соединение JSON-RPC по stdio, вызовы
методов сессии; уведомления и вопросы агента уходят приёмнику с явным
интерфейсом SessionUpdates, который заменяет рендерер ленты.

Ошибки:
AgentProcessError — процесс не запустился, не ответил на рукопожатие, умер
    или соединение с ним закрыто.
AgentRequestError — агент ответил ошибкой JSON-RPC: метод, код, текст, data.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from asyncio.subprocess import Process
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeAlias, TypeVar

from acp import Client
from acp.client.router import build_client_router
from acp.connection import Connection
from acp.exceptions import RequestError
from acp.meta import AGENT_METHODS, PROTOCOL_VERSION
from acp.schema import (
    AgentMessageChunk,
    AgentPlanContentUpdate,
    AgentPlanRemovedUpdate,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AvailableCommandsUpdate,
    CancelNotification,
    ClientCapabilities,
    CloseSessionRequest,
    CloseSessionResponse,
    ConfigOptionUpdate,
    CreateElicitationResponse,
    CreateTerminalResponse,
    CurrentModeUpdate,
    DeleteSessionRequest,
    DeleteSessionResponse,
    ElicitationMode,
    EnvVariable,
    FileSystemCapabilities,
    ForkSessionRequest,
    ForkSessionResponse,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    KillTerminalResponse,
    LoadSessionRequest,
    LoadSessionResponse,
    NewSessionRequest,
    NewSessionResponse,
    PermissionOption,
    PromptRequest,
    PromptResponse,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    ResourceContentBlock,
    SessionInfoUpdate,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)
from acp.transports import default_environment
from acp.utils import notify_model, request_model, request_model_from_dict
from pydantic import BaseModel, ValidationError

from boba.chainlit.acp.settings import AgentWorkdir, WorkdirFile
from boba.chainlit.acp.tokens import SessionServer

__all__ = [
    "AcpClientError",
    "AgentClient",
    "AgentCommand",
    "AgentProcess",
    "AgentProcessError",
    "AgentRequestError",
    "JsonRpcCode",
    "MetaKey",
    "PromptBlock",
    "SessionUpdate",
    "SessionUpdates",
]

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

SessionUpdate: TypeAlias = (
    UserMessageChunk
    | AgentMessageChunk
    | AgentThoughtChunk
    | ToolCallStart
    | ToolCallProgress
    | AgentPlanUpdate
    | AgentPlanContentUpdate
    | AgentPlanRemovedUpdate
    | AvailableCommandsUpdate
    | CurrentModeUpdate
    | ConfigOptionUpdate
    | SessionInfoUpdate
    | UsageUpdate
)

PromptBlock: TypeAlias = TextContentBlock | ResourceContentBlock


class AcpClientError(Exception):
    """Основа ошибок клиента ACP."""


class AgentProcessError(AcpClientError):
    """Процесс агента не запустился, не ответил или умер."""


class AgentRequestError(AcpClientError):
    """Агент ответил ошибкой JSON-RPC."""

    def __init__(self, method: str, code: int, message: str, data: Any) -> None:
        super().__init__(f"{method}: agent answered error {code}: {message}")
        self.method = method
        self.code = code
        self.data = data

    @property
    def method_missing(self) -> bool:
        return self.code == JsonRpcCode.METHOD_NOT_FOUND


class JsonRpcCode(IntEnum):
    """Коды ошибок JSON-RPC, которые клиент различает."""

    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    METHOD_NOT_FOUND = -32601
    RESOURCE_NOT_FOUND = -32002


class MetaKey(StrEnum):
    """Расширения агента в `_meta` запросов: точка среза ветки у
    `session/fork` и uuid записи вопроса у `session/prompt`."""

    UP_TO_UUID = "boba/upToUuid"
    MESSAGE_ID = "boba/messageId"


class ClientName(StrEnum):
    """Как клиент представляется агенту в initialize."""

    NAME = "boba-chainlit"


class SessionUpdates(Protocol):
    """Приёмник потока сессии: уведомления, вопросы разрешений и итог хода.
    Реализует FeedRenderer чата; рендерер ленты W11 встаёт на его место."""

    @abstractmethod
    async def update(self, session_id: str, update: SessionUpdate) -> None:
        """Уведомление `session/update` живого хода или воспроизведения."""

    @abstractmethod
    async def permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: Sequence[PermissionOption],
    ) -> RequestPermissionResponse:
        """Вопрос `session/request_permission`: ответ выбирает приёмник."""

    @abstractmethod
    async def turn_finished(self, session_id: str, stop_reason: str) -> None:
        """Ответ на `session/prompt` получен: ход закончен."""

    @abstractmethod
    async def abandon(self, session_id: str) -> None:
        """Ход сорвался без ответа: открытые узлы ленты закрываются."""


class AgentClient(Client):
    """Реализация Client SDK для chainlit: уведомления и вопросы — приёмнику,
    файловая система, терминал и формы не обслуживаются."""

    def __init__(self, updates: SessionUpdates) -> None:
        self._updates = updates

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        return await self._updates.permission(session_id, tool_call, options)

    async def session_update(
        self, session_id: str, update: SessionUpdate, **kwargs: Any
    ) -> None:
        await self._updates.update(session_id, update)

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> WriteTextFileResponse | None:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(  # noqa: PLR0913 — сигнатура протокола SDK
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalResponse | None:
        raise RequestError.method_not_found("terminal/kill")

    async def create_elicitation(
        self, message: str, mode: ElicitationMode, **kwargs: Any
    ) -> CreateElicitationResponse:
        raise RequestError.method_not_found("elicitation/create")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return

    def on_connect(self, conn: Any) -> None:
        return


@dataclass(frozen=True)
class AgentCommand:
    """Команда запуска агента: исполняемый файл, аргументы до `--settings`,
    нечувствительные переменные окружения поверх минимального набора."""

    executable: str
    args: Sequence[str]
    env: Mapping[str, str]
    start_timeout_sec: float
    stop_timeout_sec: float

    SETTINGS_FLAG: ClassVar[str] = "--settings"

    def argv(self, settings_path: Path) -> list[str]:
        return [self.executable, *self.args, self.SETTINGS_FLAG, str(settings_path)]

    def environment(self) -> dict[str, str]:
        merged = default_environment()
        merged.update(self.env)

        return merged

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
    """Процесс агента ACP одной сессии: запуск из приватного каталога, кадры
    JSON-RPC по его stdio, вызовы методов, остановка и удаление каталога.

    Создаётся объектом треда (ThreadAgent) и удалителем треда; stderr агента
    пишется в журнал приватного каталога и читается при отказе.
    """

    LINE_LIMIT: ClassVar[int] = 16 * 1024 * 1024
    LOG_TAIL_LINES: ClassVar[int] = 20

    def __init__(
        self, command: AgentCommand, workdir: AgentWorkdir, client: Client, version: str
    ) -> None:
        self._command = command
        self._workdir = workdir
        self._client = client
        self._version = version
        self._process: Process | None = None
        self._connection: Connection | None = None
        self._capabilities: InitializeResponse | None = None

    @property
    def workdir(self) -> AgentWorkdir:
        return self._workdir

    @property
    def pid(self) -> int:
        return self._require_process().pid

    @property
    def capabilities(self) -> InitializeResponse:
        """Ответ initialize; до рукопожатия — отказ."""
        if self._capabilities is None:
            msg = "agent process: initialize has not completed yet"
            raise AgentProcessError(msg)

        return self._capabilities

    def alive(self) -> bool:
        if self._process is None:
            return False

        return self._process.returncode is None

    async def start(self) -> InitializeResponse:
        """Запуск и рукопожатие; процесс, не ответивший в срок, гасится.

        Ошибки:
        AgentProcessError — запуск или initialize не состоялись.
        """
        argv = self._command.argv(self._workdir.settings_path)
        stderr = self._workdir.stderr_path.open("ab")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                env=self._command.environment(),
                cwd=str(self._workdir.path),
                limit=self.LINE_LIMIT,
            )
        except OSError as exc:
            msg = f"agent process {argv[0]!r} did not start: {exc}"
            raise AgentProcessError(msg) from exc
        finally:
            stderr.close()

        stdin = self._process.stdin
        stdout = self._process.stdout
        if stdin is None or stdout is None:
            msg = "agent process: asyncio gave no stdio pipes for the child"
            raise AgentProcessError(msg)

        self._connection = Connection(
            build_client_router(self._client, use_unstable_protocol=True),
            stdin,
            stdout,
        )
        try:
            self._capabilities = await asyncio.wait_for(
                self._initialize(), self._command.start_timeout_sec
            )
        except TimeoutError as exc:
            await self.close()
            msg = (
                f"agent process {argv[0]!r} (pid {self._process.pid}) did not "
                f"answer initialize within {self._command.start_timeout_sec}s: "
                f"{self.log_tail()}"
            )
            raise AgentProcessError(msg) from exc
        except AcpClientError as exc:
            await self.close()
            msg = (
                f"agent process {argv[0]!r} failed the handshake: {exc}: "
                f"{self.log_tail()}"
            )
            raise AgentProcessError(msg) from exc

        logger.info(
            "agent process started: pid=%s agent=%s/%s",
            self._process.pid,
            self._agent_name(self._capabilities),
            self._agent_version(self._capabilities),
        )

        return self._capabilities

    async def new_session(self, servers: Sequence[SessionServer]) -> str:
        """Новая сессия в приватном каталоге; id выдаёт агент."""
        request = NewSessionRequest(
            cwd=str(self._workdir.path), mcp_servers=list(servers)
        )
        response = await self._request(
            AGENT_METHODS["session_new"], request, NewSessionResponse
        )

        return response.session_id

    async def load_session(
        self, session_id: str, servers: Sequence[SessionServer]
    ) -> LoadSessionResponse:
        """Загрузка сессии с воспроизведением беседы приёмнику до ответа."""
        request = LoadSessionRequest(
            cwd=str(self._workdir.path),
            session_id=session_id,
            mcp_servers=list(servers),
        )

        return await self._request(
            AGENT_METHODS["session_load"], request, LoadSessionResponse, lenient=True
        )

    async def prompt(
        self, session_id: str, blocks: Sequence[PromptBlock], message_id: str
    ) -> PromptResponse:
        """Ход по вопросу; message_id — id сообщения chainlit, он становится
        uuid записи вопроса у агента (`boba/messageId`)."""
        request = PromptRequest(
            session_id=session_id,
            prompt=list(blocks),
            field_meta={MetaKey.MESSAGE_ID.value: message_id},
        )

        return await self._request(
            AGENT_METHODS["session_prompt"], request, PromptResponse
        )

    async def cancel(self, session_id: str) -> None:
        connection = self._require_connection()
        try:
            await notify_model(
                connection,
                AGENT_METHODS["session_cancel"],
                CancelNotification(session_id=session_id),
            )
        except ConnectionError as exc:
            msg = f"session/cancel: the agent connection is closed: {exc}"
            raise AgentProcessError(msg) from exc

    async def close_session(self, session_id: str) -> None:
        await self._request(
            AGENT_METHODS["session_close"],
            CloseSessionRequest(session_id=session_id),
            CloseSessionResponse,
            lenient=True,
        )

    async def delete_session(self, session_id: str) -> None:
        await self._request(
            AGENT_METHODS["session_delete"],
            DeleteSessionRequest(session_id=session_id),
            DeleteSessionResponse,
            lenient=True,
        )

    async def fork_session(
        self, session_id: str, servers: Sequence[SessionServer], up_to_uuid: str
    ) -> str:
        """Ветвь сессии до записи включительно; id новой сессии выдаёт агент."""
        request = ForkSessionRequest(
            session_id=session_id,
            cwd=str(self._workdir.path),
            mcp_servers=list(servers),
            field_meta={MetaKey.UP_TO_UUID.value: up_to_uuid},
        )
        response = await self._request(
            AGENT_METHODS["session_fork"], request, ForkSessionResponse
        )

        return response.session_id

    async def close(self) -> None:
        """Конец ввода, ожидание выхода, при нужде terminate и kill; каталог
        с файлом настроек удаляется в любом исходе."""
        try:
            await self._shutdown()
        finally:
            self._workdir.remove()

    def log_tail(self) -> str:
        """Хвост stderr агента: причина отказа без него не видна."""
        path = self._workdir.stderr_path
        if not path.is_file():
            return ""

        text = path.read_text(encoding=WorkdirFile.ENCODING.value, errors="replace")

        return "\n".join(text.splitlines()[-self.LOG_TAIL_LINES :])

    async def _initialize(self) -> InitializeResponse:
        request = InitializeRequest(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(
                fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
                terminal=False,
            ),
            client_info=Implementation(
                name=ClientName.NAME.value, version=self._version
            ),
        )

        return await self._request(
            AGENT_METHODS["initialize"], request, InitializeResponse
        )

    async def _request(
        self,
        method: str,
        params: BaseModel,
        response: type[ModelT],
        lenient: bool = False,
    ) -> ModelT:
        """Запрос агенту; lenient — ответ агент вправе отдать пустым (null).

        Ошибки:
        AgentRequestError — ответ-ошибка JSON-RPC.
        AgentProcessError — соединение закрыто или ответ не по схеме.
        """
        connection = self._require_connection()
        sender = request_model
        if lenient:
            sender = request_model_from_dict

        try:
            return await sender(connection, method, params, response)
        except RequestError as exc:
            raise AgentRequestError(method, exc.code, str(exc), exc.data) from exc
        except ConnectionError as exc:
            msg = f"{method}: the agent connection is closed: {exc}: {self.log_tail()}"
            raise AgentProcessError(msg) from exc
        except ValidationError as exc:
            msg = (
                f"{method}: the agent answered off the schema "
                f"{response.__name__}: {exc}"
            )
            raise AgentProcessError(msg) from exc

    async def _shutdown(self) -> None:
        process = self._process
        if process is None:
            return

        if self._connection is not None:
            await self._connection.close()

        if process.returncode is None:
            await self._close_stdin(process)

        try:
            await asyncio.wait_for(process.wait(), self._command.stop_timeout_sec)
        except TimeoutError:
            logger.warning(
                "agent process pid=%s did not exit on EOF within %ss, terminating",
                process.pid,
                self._command.stop_timeout_sec,
            )
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), self._command.stop_timeout_sec)
            except TimeoutError:
                process.kill()
                await process.wait()

        logger.info(
            "agent process exited: pid=%s code=%s", process.pid, process.returncode
        )

    @staticmethod
    async def _close_stdin(process: Process) -> None:
        stdin = process.stdin
        if stdin is None:
            return

        stdin.close()
        try:
            await stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _require_process(self) -> Process:
        if self._process is None:
            msg = "agent process has not been started"
            raise AgentProcessError(msg)

        return self._process

    def _require_connection(self) -> Connection:
        if self._connection is None:
            msg = "agent process has not been started"
            raise AgentProcessError(msg)

        return self._connection

    @staticmethod
    def _agent_name(capabilities: InitializeResponse) -> str:
        if capabilities.agent_info is None:
            return "?"

        return capabilities.agent_info.name

    @staticmethod
    def _agent_version(capabilities: InitializeResponse) -> str:
        if capabilities.agent_info is None:
            return "?"

        return capabilities.agent_info.version
