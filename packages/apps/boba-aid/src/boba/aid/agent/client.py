"""ACP-клиент чата aid на v2: соединение ClientConnection с агентом — по
stdio своего процесса (AgentProcess) или по сети со службой агента
(AgentService, streamable HTTP с входом proxy), — методы сессии;
уведомления, вопросы разрешений и формы `elicitation/create` агента уходят
приёмнику с явным интерфейсом SessionUpdates, который заменяет рендерер
ленты, а запросы mcp/message — ретранслятору (McpRelay). Клиент объявляет
агенту формы (`elicitation.form`). Режим разрешений каждой открытой сессии
(`session/new|resume|fork`) клиент выставляет по ACP: настройка категории
mode из `configOptions` ответа и `session/set_config_option`
(SessionModeSelector); в службе он прежде выбирает профиль агента настройкой
категории model (SessionProfileSelector).

Ошибки:
AgentProcessError — процесс не запустился или вход в службу не состоялся, не
    прошёл рукопожатие (чужая версия ACP, нет серверов type "acp"), не
    ответил на метод сессии в срок, умер или соединение с ним закрыто;
    служба не предлагает профиль чата.
AgentRequestError — агент ответил ошибкой JSON-RPC: метод, код, текст, data.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from asyncio.subprocess import Process
from collections.abc import Awaitable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeAlias, TypeVar
from uuid import UUID

import httpx2
from acp.exceptions import RequestError
from acp.experimental.v2 import Client, schema
from acp.http import create_http_stream
from acp.http.client import AcpHttpStatusError
from acp.transports import spawn_stdio_transport
from mcp.client.auth.exceptions import OAuthFlowError
from pydantic import AnyUrl, BaseModel, ValidationError

from boba.aid.agent.config import AgentServiceConfig
from boba.aid.agent.relay import ClientName, McpRelay
from boba.aid.agent.settings import AgentWorkdir, WorkdirFile
from boba.aid.rendering.acp_view import SessionUpdate
from boba.mcp_client.client import McpCaller, McpClientError
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
    "AcpModeOption",
    "AgentClient",
    "AgentCommand",
    "AgentLink",
    "AgentProcess",
    "AgentProcessError",
    "AgentRequestError",
    "AgentService",
    "ConfigOption",
    "ElicitationReply",
    "ModeChange",
    "PromptBlock",
    "SessionModeSelector",
    "SessionProfileSelector",
    "SessionUpdates",
]

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

PromptBlock: TypeAlias = schema.TextContentBlock | schema.ResourceContentBlock
ElicitationReply: TypeAlias = (
    schema.AcceptElicitationResponse
    | schema.DeclineElicitationResponse
    | schema.CancelElicitationResponse
)


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
    """Приёмник потока сессии: уведомления, вопросы разрешений, формы
    вопросов пользователю и итог хода. Реализуют приёмник ленты (AgentFeed),
    приёмник без ленты (MuteUpdates) и обёртка агента треда (AskedQuestions)."""

    @abstractmethod
    async def update(self, session_id: str, update: SessionUpdate) -> None:
        """Уведомление `session/update` живого хода или воспроизведения."""

    @abstractmethod
    async def permission(
        self, session_id: str, title: str, options: Sequence[schema.PermissionOption]
    ) -> schema.RequestPermissionResponse:
        """Вопрос `session/request_permission`: ответ выбирает приёмник."""

    @abstractmethod
    async def elicitation(
        self, request: schema.CreateFormSessionElicitationRequest
    ) -> ElicitationReply:
        """Форма `elicitation/create` агента: вопрос пользователю в рамках
        сессии и вызова; ответ — принятая форма, отказ или отмена.

        Ошибки:
        RequestError — ответ вкладки не по форме ответа.
        """

    @abstractmethod
    async def turn_finished(
        self, session_id: str, idle: schema.IdleSessionStateUpdate
    ) -> None:
        """Агент сообщил idle: ход закончен с причиной stopReason."""

    @abstractmethod
    async def abandon(self, session_id: str) -> None:
        """Ход сорвался без idle: открытые узлы ленты закрываются."""


class AgentClient(Client):
    """Реализация Client SDK v2 для чата aid: уведомления, вопросы
    разрешений и формы `elicitation/create` — приёмнику; остальные методы
    протокола клиент не обслуживает, и SDK сам отвечает на них
    method_not_found."""

    FORM: ClassVar[str] = "form"
    """Единственный вид форм, который объявляет клиент."""

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

    async def create_elicitation(  # noqa: PLR0913 — сигнатура протокола SDK
        self,
        message: str,
        mode: str,
        *,
        session_id: str | None = None,
        request_id: int | str | None = None,
        tool_call_id: str | None = None,
        requested_schema: schema.ElicitationSchema | None = None,
        elicitation_id: str | None = None,
        url: str | AnyUrl | None = None,
        **kwargs: Any,
    ) -> ElicitationReply:
        """Форма сессии — приёмнику; `_meta` запроса SDK отдаёт именованными
        аргументами kwargs. Клиент объявил только формы: запрос другого вида
        или без сессии — ошибка -32602.

        Ошибки:
        RequestError — запрос не форма сессии либо ответ вкладки не по
            форме ответа.
        """
        if mode != self.FORM:
            details = (
                f"elicitation/create of mode {mode!r} is refused: this client "
                f"declared the {self.FORM!r} mode only"
            )
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        if session_id is None:
            details = (
                "elicitation/create without sessionId is refused: this client "
                "asks the user only within a session"
            )
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        if requested_schema is None:
            details = (
                f"elicitation/create of session {session_id}: a form needs "
                "requestedSchema, got none"
            )
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        meta: dict[str, Any] | None = None
        if kwargs:
            meta = dict(kwargs)

        request = schema.CreateFormSessionElicitationRequest(
            message=message,
            session_id=session_id,
            tool_call_id=tool_call_id,
            requested_schema=requested_schema,
            field_meta=meta,
        )

        return await self._updates.elicitation(request)

    # SDK считает метод протокола, оставленный как есть, неподдержанным
    connect_mcp = Client.connect_mcp
    mcp_message = Client.mcp_message
    notify_mcp = Client.notify_mcp
    disconnect_mcp = Client.disconnect_mcp
    complete_elicitation = Client.complete_elicitation


ConfigOption: TypeAlias = (
    schema.SelectSessionConfigOption
    | schema.BooleanSessionConfigOption
    | schema.OtherSessionConfigOption
)


class AcpModeOption(StrEnum):
    """Настройка режима разрешений ACP: категория `configOptions`, по которой
    чат находит настройку у любого агента, и режим, который чат включает."""

    CATEGORY = "mode"
    BYPASS = "bypassPermissions"


@dataclass(frozen=True)
class ModeChange:
    """Смена настройки сессии: id настройки и значение для
    `session/set_config_option`."""

    config_id: str
    value: str


class SessionModeSelector:
    """Решение о режиме разрешений открытой сессии по `configOptions` ответа
    агента: у настройки категории mode, предлагающей bypassPermissions, он
    включается, если ещё не действует; агент без такой настройки или без
    этого режима остаётся в своём — его вопросы разрешений чат отвечает
    согласием (TurnRenderer.permission). Файл настроек даёт лишь потолок
    режимов (allowBypass). Создаёт AgentProcess, запрос шлёт он."""

    def change(
        self, session_id: str, options: Sequence[ConfigOption] | None
    ) -> ModeChange | None:
        """Смена режима, которую стоит запросить; None — менять нечего."""
        option = self._mode_option(options)
        if option is None:
            logger.info(
                "session %s: the agent declares no permission mode option; "
                "its own mode stays",
                session_id,
            )
            return None

        if option.current_value == AcpModeOption.BYPASS.value:
            return None

        values = list(self.values(option))
        if AcpModeOption.BYPASS.value not in values:
            logger.warning(
                "session %s: the agent offers permission modes %s without %r; "
                "its mode %r stays and permission questions are answered by the chat",
                session_id,
                values,
                AcpModeOption.BYPASS.value,
                option.current_value,
            )
            return None

        return ModeChange(config_id=option.config_id, value=AcpModeOption.BYPASS.value)

    @staticmethod
    def _mode_option(
        options: Sequence[ConfigOption] | None,
    ) -> schema.SelectSessionConfigOption | None:
        if options is None:
            return None

        for option in options:
            if not isinstance(option, schema.SelectSessionConfigOption):
                continue

            if option.category == AcpModeOption.CATEGORY.value:
                return option

        return None

    @staticmethod
    def values(option: schema.SelectSessionConfigOption) -> Iterator[str]:
        """Значения настройки: варианты списком или группами."""
        for item in option.options:
            if isinstance(item, schema.SessionConfigSelectOption):
                yield item.value
                continue

            for nested in item.options:
                yield nested.value


@dataclass(frozen=True)
class AgentCommand:
    """Команда запуска агента: исполняемый файл и аргументы до `--settings`,
    сроки рукопожатия, ответа на метод сессии и выхода. Окружение ребёнка —
    минимальный набор SDK: настройки агенту несёт только файл."""

    executable: str
    args: Sequence[str]
    start_timeout_sec: float
    request_timeout_sec: float
    stop_timeout_sec: float

    SETTINGS_FLAG: ClassVar[str] = "--settings"

    def argv(self, settings_path: Path) -> list[str]:
        return [*self.args, self.SETTINGS_FLAG, str(settings_path)]


class AgentLink:
    """Соединение чата с агентом ACP v2 одной вкладки: рукопожатие и методы
    сессии поверх ClientConnection SDK.

    Базовый класс: его наследуют AgentProcess (свой процесс агента по stdio)
    и AgentService (служба агента по сети); наследник открывает соединение,
    называет рабочий каталог сессий и знает, жив ли собеседник. Запросы
    mcp/message агента уходят ретранслятору вместе с отправкой уведомлений
    этого соединения; режим разрешений каждой открытой сессии выставляется
    по ACP.
    """

    LINK_ERRORS: ClassVar[tuple[type[Exception], ...]] = (ConnectionError,)
    """Обрыв соединения с агентом: его транспорт бросает такие ошибки."""

    def __init__(
        self,
        client: Client,
        relay: McpRelay,
        servers: Sequence[schema.AcpMcpServer],
        version: str,
        request_timeout_sec: float,
    ) -> None:
        self._client = client
        self._relay = relay
        self._servers = list(servers)
        self._version = version
        self._request_timeout_sec = request_timeout_sec
        self._mode = SessionModeSelector()
        self._stack = AsyncExitStack()
        self._connection: ClientConnection | None = None
        self._forks_by_message = False
        """Агент объявил срез ветки session/fork по messageId вопроса."""

    @abstractmethod
    def label(self) -> str:
        """Кто на том конце: процесс с pid или служба с адресом."""

    @abstractmethod
    def alive(self) -> bool:
        """Собеседник ещё ответит."""

    @abstractmethod
    async def ended(self) -> None:
        """Возвращается, когда собеседник ушёл: процесс вышел или служба
        закрыла соединение."""

    @abstractmethod
    def log_tail(self) -> str:
        """Что известно о причине отказа собеседника."""

    @abstractmethod
    async def start(self) -> schema.InitializeResponse:
        """Соединение и рукопожатие.

        Ошибки:
        AgentProcessError — соединение не открылось или рукопожатие не прошло.
        """

    @abstractmethod
    async def close(self) -> None:
        """Соединение закрывается, ресурсы наследника освобождаются."""

    @abstractmethod
    def _cwd(self) -> str:
        """Рабочий каталог сессий (cwd методов session/*)."""

    async def _handshake(
        self, connection: ClientConnection, start_timeout_sec: float
    ) -> schema.InitializeResponse:
        """Рукопожатие v2 в срок; агент без серверов type "acp" не годится.

        Ошибки:
        AgentProcessError — срок вышел, версия чужая или нет mcp.acp.
        """
        self._connection = connection
        try:
            response = await asyncio.wait_for(self._initialize(), start_timeout_sec)
        except TimeoutError as exc:
            msg = (
                f"{self.label()} did not answer initialize within "
                f"{start_timeout_sec}s: {self.log_tail()}"
            )
            raise AgentProcessError(msg) from exc
        except AcpClientError as exc:
            msg = (
                f"{self.label()} failed the ACP handshake, this chat speaks ACP "
                f"version {AcpVersion.V2.value}: {exc}: {self.log_tail()}"
            )
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
                f"{self.label()}: agent {response.info.name!r} does not declare "
                "capabilities.session.mcp.acp in initialize, expected it to take "
                "the MCP servers of the chat as servers of type 'acp'"
            )
            raise AgentProcessError(msg)

        logger.info(
            "%s: agent=%s/%s", self.label(), response.info.name, response.info.version
        )

        return response

    async def open_session(self) -> schema.NewSessionResponse:
        """Новая сессия с серверами соединения; id и настройки
        (`configOptions`) выдаёт агент, режим разрешений по ним выставляет
        клиент."""
        connection = self._require_connection()

        opened = await self._request(
            "session/new",
            connection.new_session(cwd=self._cwd(), mcp_servers=list(self._servers)),
        )
        await self._configure(opened.session_id, opened.config_options)

        return opened

    async def resume_session(
        self, session_id: str, replay: bool
    ) -> schema.ResumeSessionResponse:
        """Открытие сохранённой сессии; replay — беседа воспроизводится
        приёмнику с начала до ответа, иначе прошлых ходов он не получает."""
        connection = self._require_connection()
        replay_from: schema.ReplayFromStartVariant | None = None
        if replay:
            replay_from = schema.ReplayFromStartVariant()

        resumed = await self._request(
            "session/resume",
            connection.resume_session(
                session_id=session_id,
                cwd=self._cwd(),
                mcp_servers=list(self._servers),
                replay_from=replay_from,
            ),
        )
        await self._configure(session_id, resumed.config_options)

        return resumed

    async def set_config_option(
        self, session_id: str, config_id: str, value: str
    ) -> schema.SetSessionConfigOptionResponse:
        """Настройка сессии `session/set_config_option`: ответ — полный список
        настроек после смены."""
        connection = self._require_connection()

        return await self._request(
            "session/set_config_option",
            connection.set_config_option(
                config_id=config_id, session_id=session_id, value=value
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

        forked = await self._request(
            "session/fork",
            connection.fork_session(
                session_id=session_id,
                cwd=self._cwd(),
                mcp_servers=list(self._servers),
                **meta,
            ),
        )
        await self._configure(forked.session_id, forked.config_options)

        return forked

    async def _configure(
        self, session_id: str, options: Sequence[ConfigOption] | None
    ) -> None:
        """Настройки открытой сессии по её `configOptions`: режим разрешений.

        Ошибки:
        AgentRequestError — агент отказал в `session/set_config_option`.
        AgentProcessError — агент не ответил в срок или умер.
        """
        change = self._mode.change(session_id, options)
        if change is None:
            return

        await self.set_config_option(session_id, change.config_id, change.value)
        logger.info(
            "session %s: permission mode %r set over ACP", session_id, change.value
        )

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

        elicitation = schema.ElicitationCapabilities(
            form=schema.ElicitationFormCapabilities()
        )

        return await self._answer_of(
            "initialize",
            connection.initialize(
                protocol_version=AcpVersion.V2.value,
                info=info,
                capabilities=schema.ClientCapabilities(elicitation=elicitation),
            ),
        )

    async def _request(self, method: str, call: Awaitable[ModelT]) -> ModelT:
        """Ответ агента на метод сессии method в срок request_timeout_sec.

        Ошибки:
        AgentRequestError — ответ-ошибка JSON-RPC.
        AgentProcessError — агент не ответил в срок, соединение закрыто или
            ответ не по схеме.
        """
        limit = self._request_timeout_sec
        try:
            async with asyncio.timeout(limit):
                return await self._answer_of(method, call)
        except TimeoutError as exc:
            msg = (
                f"{method}: expected {self.label()} to answer within {limit}s "
                f"([agent.process].request_timeout_sec), got no answer: "
                f"{self.log_tail()}"
            )
            raise AgentProcessError(msg) from exc

    async def _answer_of(self, method: str, call: Awaitable[ModelT]) -> ModelT:
        """Ответ агента на метод method без своего срока.

        Ошибки:
        AgentRequestError — ответ-ошибка JSON-RPC.
        AgentProcessError — соединение закрыто или ответ не по схеме.
        """
        try:
            return await call
        except RequestError as exc:
            raise AgentRequestError(method, exc.code, str(exc), exc.data) from exc
        except self.LINK_ERRORS as exc:
            msg = (
                f"{method}: the connection to {self.label()} is closed: {exc}: "
                f"{self.log_tail()}"
            )
            raise AgentProcessError(msg) from exc
        except ValidationError as exc:
            msg = f"{method}: the agent answered off the ACP v2 schema: {exc}"
            raise AgentProcessError(msg) from exc

    def _require_connection(self) -> ClientConnection:
        if self._connection is None:
            msg = (
                f"{self.label()}: an ACP request needs an open connection, got "
                "none: start() was not called"
            )
            raise AgentProcessError(msg)

        return self._connection


class AgentProcess(AgentLink):
    """Реализация AgentLink своим процессом агента одной вкладки: запуск из
    приватного каталога, соединение ClientConnection по его stdio,
    остановка и удаление каталога.

    Создаётся запуском агента (AgentLauncher) в режиме процесса с
    объявлениями серверов процесса. Процессом управляет
    spawn_stdio_transport SDK; stderr агента пишется в журнал приватного
    каталога и читается при отказе.
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
        super().__init__(client, relay, servers, version, command.request_timeout_sec)
        self._command = command
        self._workdir = workdir
        self._process: Process | None = None

    @property
    def workdir(self) -> AgentWorkdir:
        return self._workdir

    @property
    def pid(self) -> int:
        return self._require_process().pid

    def label(self) -> str:
        if self._process is None:
            return f"agent process {self._command.executable!r} (not started)"

        return f"agent process {self._command.executable!r} (pid {self._process.pid})"

    def alive(self) -> bool:
        """Процесс не вышел и его вывод не кончился: агент, закрывший stdout,
        ответить уже не сможет, хотя процесс ещё жив."""
        if self._process is None:
            return False

        if self._process.returncode is not None:
            return False

        if self._connection is None:
            return False

        return self._connection.connected()

    async def exited(self) -> int:
        """Код выхода процесса, когда он завершится."""
        return await self._require_process().wait()

    async def ended(self) -> None:
        await self.exited()

    def _cwd(self) -> str:
        return str(self._workdir.path)

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
            self._workdir.remove()
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
                        cwd=self._workdir.path,
                        stderr=stderr.fileno(),
                        limit=self.LINE_LIMIT,
                        shutdown_timeout=self._command.stop_timeout_sec,
                    )
                )
            except OSError as exc:
                self._workdir.remove()
                msg = (
                    f"agent process {executable!r} did not start from "
                    f"{self._workdir.path}: {exc}; check [env].agent_command"
                )
                raise AgentProcessError(msg) from exc

        self._process = process
        connection = ClientConnection(self._client, self._answer, stdin, stdout)
        # соединение закрывается раньше процесса: выход из стека — в обратном порядке
        self._stack.push_async_callback(connection.close)
        try:
            return await self._handshake(connection, self._command.start_timeout_sec)
        except AgentProcessError:
            await self.close()
            raise

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

    def _require_process(self) -> Process:
        if self._process is None:
            msg = (
                f"agent process {self._command.executable!r} in "
                f"{self._workdir.path}: its pid and exit need a started process, "
                "got none: start() was not called"
            )
            raise AgentProcessError(msg)

        return self._process


class SessionProfileSelector:
    """Профиль агента открытой сессии в службе агента: настройка категории
    model из `configOptions` получает имя профиля чата. Создаёт AgentService,
    запрос шлёт он."""

    CATEGORY: ClassVar[str] = "model"

    def __init__(self, profile: str) -> None:
        self._profile = profile

    def change(
        self, session_id: str, options: Sequence[ConfigOption] | None
    ) -> ModeChange | None:
        """Смена профиля, которую стоит запросить; None — он уже выбран.

        Ошибки:
        AgentProcessError — служба не предлагает профиль чата.
        """
        option = self._option(options)
        if option is None:
            raise AgentProcessError(self._missing(session_id, []))

        offered = list(SessionModeSelector.values(option))
        if self._profile not in offered:
            raise AgentProcessError(self._missing(session_id, offered))

        if option.current_value == self._profile:
            return None

        return ModeChange(config_id=option.config_id, value=self._profile)

    def _missing(self, session_id: str, offered: Sequence[str]) -> str:
        return (
            f"session {session_id}: the agent service offers model profiles "
            f"{list(offered)}, the chat profile {self._profile!r} is not among "
            f"them; declare [agent.profiles.{self._profile}] in the service and "
            "grant it to the roles of the user"
        )

    def _option(
        self, options: Sequence[ConfigOption] | None
    ) -> schema.SelectSessionConfigOption | None:
        if options is None:
            return None

        for option in options:
            if not isinstance(option, schema.SelectSessionConfigOption):
                continue

            if option.category == self.CATEGORY:
                return option

        return None


class AgentService(AgentLink):
    """Реализация AgentLink службой агента (boba-agent-service): соединение
    ACP по streamable HTTP SDK с входом proxy от имени пользователя вкладки.

    Создаётся запуском агента (AgentLauncher) в режиме службы. Процесс
    агента под каждую сессию запускает служба, профили моделей и историю
    держит она; чат выбирает профиль агента настройкой сессии `model` по
    имени профиля чата. Токен службы получает штатный провайдер OAuth
    (ProxyAuth) по отказу 401 и обновляет его сам.
    """

    LINK_ERRORS: ClassVar[tuple[type[Exception], ...]] = (
        ConnectionError,
        AcpHttpStatusError,
        httpx2.HTTPError,
        McpClientError,
        OAuthFlowError,
    )
    """Обрыв соединения, отказ HTTP и несостоявшийся вход в службу."""
    WATCH_SEC: ClassVar[float] = 1.0
    CWD: ClassVar[str] = "/"
    """Рабочий каталог сессий: каталогов чата у службы нет."""

    def __init__(  # noqa: PLR0913 — соединение собирается всеми своими входами
        self,
        config: AgentServiceConfig,
        caller: McpCaller,
        profile: str,
        client: Client,
        relay: McpRelay,
        servers: Sequence[schema.AcpMcpServer],
        version: str,
    ) -> None:
        super().__init__(client, relay, servers, version, config.request_timeout_sec)
        self._config = config
        self._caller = caller
        self._url = config.endpoint.url()
        self._profile = SessionProfileSelector(profile)

    def label(self) -> str:
        return f"agent service {self._url} [user={self._caller.login}]"

    def alive(self) -> bool:
        if self._connection is None:
            return False

        return self._connection.connected()

    async def ended(self) -> None:
        while self.alive():
            await asyncio.sleep(self.WATCH_SEC)

    def log_tail(self) -> str:
        return "the agent service log names the cause"

    def _cwd(self) -> str:
        return self.CWD

    async def start(self) -> schema.InitializeResponse:
        """Ошибки:
        AgentProcessError — вход, соединение или рукопожатие не состоялись.
        """
        auth = self._config.auth.on_behalf_of(self._caller).httpx_auth(self._url)
        http = httpx2.AsyncClient(auth=auth, timeout=httpx2.Timeout(None))
        self._stack.push_async_callback(http.aclose)
        transport = create_http_stream(self._url, client=http)
        connection = ClientConnection(self._client, self._answer, transport, None)
        self._stack.push_async_callback(connection.close)
        try:
            return await self._handshake(connection, self._config.start_timeout_sec)
        except AgentProcessError:
            await self.close()
            raise

    async def close(self) -> None:
        await self._stack.aclose()

    async def _configure(
        self, session_id: str, options: Sequence[ConfigOption] | None
    ) -> None:
        """Профиль агента по имени профиля чата, затем режим разрешений.

        Ошибки:
        AgentProcessError — службы не предлагает профиль, не ответила или ушла.
        AgentRequestError — служба отказала в `session/set_config_option`.
        """
        change = self._profile.change(session_id, options)
        if change is not None:
            changed = await self.set_config_option(
                session_id, change.config_id, change.value
            )
            options = changed.config_options
            logger.info(
                "session %s: agent profile %r selected", session_id, change.value
            )

        await super()._configure(session_id, options)
