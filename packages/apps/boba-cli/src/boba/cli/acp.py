"""Вход ACP: агент `boba acp` над ядром boba-agent на SDK agent-client-protocol.

Сервер JSON-RPC по stdio: `initialize`, `session/new`, `session/prompt` с
потоком `session/update`, `session/cancel`, `$/cancel_request`, запрос
`session/request_permission` к клиенту. Сессию открывает сборка
(SessionAssembly): агент ядра с серверами MCP из запроса; события ядра
приёмник SessionRelay переводит в уведомления по таблице AcpMapping, а
ответ на `session/prompt` уходит после них, когда ядро отдало итог хода.
Второй `prompt` в идущем ходе встаёт в очередь ввода ядра и получает ответ
по завершении своего хода; `session/cancel` прерывает ход и очищает
очередь. Методы истории, настроек и ветвления (`session/load|resume|list|
close|delete|fork`, `set_mode`, `set_config_option`, `authenticate`)
объявлены в возможностях и отвечают «метод не найден» — точки расширения.
Всё своё в протоколе лежит в `_meta` под ключами с префиксом `boba/`.

Ошибки:
RequestError — ошибка JSON-RPC клиенту: параметры запроса (-32602),
    неизвестная сессия (-32002), нереализованный метод (-32601), отказ
    модели или сборки сессии (-32603).
SessionRefusedError — сборка сессии отвергла параметры запроса; поднимает
    реализация SessionAssembly, вход переводит в -32602.
SessionOpenError — сессию открыть не удалось: хранилище, сервер MCP,
    профиль; поднимает реализация SessionAssembly, вход переводит в -32603.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeAlias

from acp import Agent as AcpAgentProtocol
from acp import Client as AcpClientProtocol
from acp.agent.router import build_agent_router
from acp.connection import Connection, StreamDirection
from acp.connection import StreamEvent as FrameEvent
from acp.exceptions import RequestError
from acp.helpers import (
    start_tool_call,
    text_block,
    tool_content,
    update_agent_message,
    update_agent_thought,
    update_tool_call,
)
from acp.meta import PROTOCOL_VERSION
from acp.router import Route
from acp.schema import (
    AcpMcpServer,
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    AudioContentBlock,
    AuthenticateResponse,
    CancelRequestNotification,
    ClientCapabilities,
    CloseSessionResponse,
    EmbeddedResourceContentBlock,
    ForkSessionResponse,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    McpServerHttp,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PermissionOptionKind,
    PromptCapabilities,
    PromptResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
    ResourceContentBlock,
    ResumeSessionResponse,
    SelectedPermissionOutcome,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionDeleteCapabilities,
    SessionForkCapabilities,
    SessionListCapabilities,
    SessionNotification,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    SetSessionModeResponse,
    SseMcpServer,
    StopReason,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallStatus,
    ToolCallUpdate,
    ToolKind,
)
from acp.utils import notify_model, request_model
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from boba.agent.agent import Agent
from boba.agent.control import (
    AllowAnswer,
    CanUseToolRequest,
    DenyAnswer,
    PermissionAnswer,
)
from boba.agent.events import (
    AgentEvent,
    ControlCancelEvent,
    ControlRequestEvent,
    ErrorEvent,
    EventSink,
    InitEvent,
    RecordEvent,
    ResultEvent,
    StreamEvent,
    ToolProgressEvent,
    ToolStartedEvent,
    TurnOutcome,
)
from boba.agent.history import SessionId
from boba.agent.ids import IdMint
from boba.agent.queue import QueueEntry, QueueKind, QueuePriority, QueueRefusedError
from boba.agent.records import (
    AssistantRecord,
    Record,
    ToolResultBlock,
    ToolUseBlock,
    UserRecord,
)
from boba.agent.session import SessionState
from boba.cancellation import StopReason as CoreStopReason
from boba.cli.settings import McpServerJson, McpTransportJson
from boba.toolkit.dag import ToolHints

logger = logging.getLogger(__name__)

__all__ = [
    "AcpAgent",
    "AcpMapping",
    "AcpMethod",
    "AcpServer",
    "ClientLink",
    "McpServerSpecs",
    "MetaKey",
    "OpenedSession",
    "PermissionChoice",
    "PromptText",
    "SessionAssembly",
    "SessionOpenError",
    "SessionRefusedError",
    "SessionRelay",
    "StopReasonName",
    "ToolKindName",
    "ToolStatusName",
]

PromptBlock: TypeAlias = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)
McpServerBlock: TypeAlias = HttpMcpServer | SseMcpServer | AcpMcpServer | McpServerStdio
Update: TypeAlias = (
    AgentMessageChunk | AgentThoughtChunk | ToolCallStart | ToolCallProgress
)


class SessionRefusedError(Exception):
    """Сборка сессии отвергла параметры запроса."""


class SessionOpenError(Exception):
    """Сессию открыть не удалось: хранилище, сервер MCP, профиль."""


class AcpMethod(StrEnum):
    """Методы JSON-RPC, которые вход называет сам: кроме маршрутов SDK."""

    SESSION_PROMPT = "session/prompt"
    SESSION_UPDATE = "session/update"
    REQUEST_PERMISSION = "session/request_permission"
    CANCEL_REQUEST = "$/cancel_request"
    SESSION_LOAD = "session/load"
    SESSION_RESUME = "session/resume"
    SESSION_LIST = "session/list"
    SESSION_CLOSE = "session/close"
    SESSION_FORK = "session/fork"
    SESSION_SET_MODE = "session/set_mode"
    SESSION_SET_CONFIG_OPTION = "session/set_config_option"
    AUTHENTICATE = "authenticate"


class MetaKey(StrEnum):
    """Ключи `_meta` расширений этого агента; префикс `boba/` — по правилу
    расширяемости ACP."""

    PROGRESS = "boba/progress"


class StopReasonName(StrEnum):
    """Причины конца хода ACP (`stopReason`)."""

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"


class ToolKindName(StrEnum):
    """Виды инструментов ACP (`kind`), которые агент выводит из пометок."""

    READ = "read"
    EDIT = "edit"
    EXECUTE = "execute"
    OTHER = "other"


class ToolStatusName(StrEnum):
    """Состояния вызова ACP (`status`)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class PermissionChoice(StrEnum):
    """Варианты ответа на `session/request_permission`: `optionId` совпадает
    с видом варианта ACP."""

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    REJECT_ONCE = "reject_once"

    @property
    def label(self) -> str:
        if self is PermissionChoice.ALLOW_ONCE:
            return "Allow"

        if self is PermissionChoice.ALLOW_ALWAYS:
            return "Allow and remember"

        return "Reject"


class ErrorDataKey(StrEnum):
    """Ключи `data` ошибки JSON-RPC, которые пишет агент."""

    KIND = "kind"
    MESSAGE = "message"
    DETAILS = "details"
    SESSION_ID = "sessionId"


class ProgressMeta(BaseModel):
    """Расширение `boba/progress` в `_meta` уведомления `tool_call_update`:
    отчёт сервера MCP о ходе вызова, которому в ACP поля нет."""

    model_config = ConfigDict(frozen=True)

    message: str
    progress: float
    total: float | None = None
    elapsed_sec: float


class Frame(BaseModel):
    """Кадр JSON-RPC глазами наблюдателя соединения: только поля адресации."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: int | str | None = None
    method: str | None = None
    params: Mapping[str, Any] | None = None


class SessionParams(BaseModel):
    """Параметры запроса, у которого есть сессия."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    session_id: str = Field(alias="sessionId")


class WireLiterals:
    """Строгие литералы SDK из перечислений входа: значение проходит адаптер
    pydantic на границе, а не приводится типом."""

    def __init__(self) -> None:
        self._stop_reasons = TypeAdapter(StopReason)
        self._kinds = TypeAdapter(ToolKind)
        self._statuses = TypeAdapter(ToolCallStatus)
        self._option_kinds = TypeAdapter(PermissionOptionKind)

    def stop_reason(self, name: StopReasonName) -> StopReason:
        return self._stop_reasons.validate_python(name.value)

    def kind(self, name: ToolKindName) -> ToolKind:
        return self._kinds.validate_python(name.value)

    def status(self, name: ToolStatusName) -> ToolCallStatus:
        return self._statuses.validate_python(name.value)

    def option_kind(self, choice: PermissionChoice) -> PermissionOptionKind:
        return self._option_kinds.validate_python(choice.value)


class ToolTitles:
    """Заголовок вызова для клиента: имя инструмента и краткие аргументы."""

    LIMIT: ClassVar[int] = 60
    ELLIPSIS: ClassVar[str] = "…"

    def title(self, name: str, arguments: Mapping[str, Any]) -> str:
        if not arguments:
            return name

        rendered = json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True)
        if len(rendered) > self.LIMIT:
            rendered = rendered[: self.LIMIT] + self.ELLIPSIS

        return f"{name} {rendered}"


class PermissionAsk(BaseModel):
    """Запрос разрешения клиенту: вызов и варианты ответа."""

    model_config = ConfigDict(frozen=True)

    tool_call: ToolCallUpdate
    options: Sequence[PermissionOption]


class AcpMapping:
    """Соответствие ядра протоколу: исходы хода → `stopReason`, события →
    уведомления `session/update`, пометки инструмента → `kind`, вопрос
    `can_use_tool` → запрос разрешения и ответ на него.

    Единственное место, где ядро встречается со схемой ACP; создаёт вход и
    отдаёт приёмникам событий сессий.
    """

    STOP_REASONS: ClassVar[Mapping[TurnOutcome, StopReasonName]] = {
        TurnOutcome.COMPLETED: StopReasonName.END_TURN,
        TurnOutcome.MAX_TURNS: StopReasonName.MAX_TURN_REQUESTS,
        TurnOutcome.OUTPUT_LIMIT: StopReasonName.MAX_TOKENS,
        TurnOutcome.ABORTED_STREAMING: StopReasonName.CANCELLED,
        TurnOutcome.ABORTED_TOOLS: StopReasonName.CANCELLED,
        TurnOutcome.SHUTDOWN: StopReasonName.CANCELLED,
        TurnOutcome.BLOCKING_LIMIT: StopReasonName.REFUSAL,
        TurnOutcome.COMPACT_FAILURE_BREAKER: StopReasonName.REFUSAL,
        TurnOutcome.RAPID_REFILL_BREAKER: StopReasonName.REFUSAL,
    }

    def __init__(self) -> None:
        self._wire = WireLiterals()
        self._titles = ToolTitles()

    def prompt_response(self, result: ResultEvent) -> PromptResponse:
        """Ответ на `session/prompt` по итогу хода.

        Ошибки:
        RequestError — модель отказала: ход кончился без ответа (-32603).
        """
        found = self.STOP_REASONS.get(result.outcome)
        if found is None:
            data = {
                ErrorDataKey.KIND.value: result.outcome.value,
                ErrorDataKey.MESSAGE.value: result.error,
            }
            raise RequestError.internal_error(data)

        return PromptResponse(stop_reason=self._wire.stop_reason(found))

    def cancelled_response(self) -> PromptResponse:
        return PromptResponse(
            stop_reason=self._wire.stop_reason(StopReasonName.CANCELLED)
        )

    def tool_kind(self, hints: ToolHints | None) -> ToolKindName:
        """`kind` по пометкам MCP: читающий — read, разрушающий — execute,
        меняющий — edit; инструмент без карточки — other."""
        if hints is None:
            return ToolKindName.OTHER

        if hints.read_only:
            return ToolKindName.READ

        if hints.destructive:
            return ToolKindName.EXECUTE

        return ToolKindName.EDIT

    def chunks(self, event: StreamEvent) -> Sequence[Update]:
        updates: list[Update] = []
        if event.reasoning:
            thought = update_agent_thought(text_block(event.reasoning))
            thought.message_id = event.message_id
            updates.append(thought)

        if event.content:
            message = update_agent_message(text_block(event.content))
            message.message_id = event.message_id
            updates.append(message)

        return updates

    def tool_call(self, block: ToolUseBlock, hints: ToolHints | None) -> ToolCallStart:
        return start_tool_call(
            block.id,
            self._titles.title(block.name, block.input),
            kind=self._wire.kind(self.tool_kind(hints)),
            status=self._wire.status(ToolStatusName.PENDING),
            raw_input=dict(block.input),
        )

    def tool_results(self, record: UserRecord) -> Sequence[ToolCallProgress]:
        updates: list[ToolCallProgress] = []
        for block in record.message.tool_results():
            updates.append(self._tool_result(block, record.tool_use_result))

        return updates

    def _tool_result(self, block: ToolResultBlock, artifact: Any) -> ToolCallProgress:
        status = ToolStatusName.COMPLETED
        if block.is_error:
            status = ToolStatusName.FAILED

        return update_tool_call(
            block.tool_use_id,
            status=self._wire.status(status),
            content=[tool_content(text_block(block.text()))],
            raw_output=artifact,
        )

    def tool_started(self, event: ToolStartedEvent) -> ToolCallProgress:
        return update_tool_call(
            event.tool_use_id, status=self._wire.status(ToolStatusName.IN_PROGRESS)
        )

    def tool_progress(self, event: ToolProgressEvent) -> ToolCallProgress:
        """Отчёт о ходе вызова: статус стандартный, поля отчёта — в `_meta`
        под ключом `boba/progress` (расширение)."""
        meta = ProgressMeta(
            message=event.message,
            progress=event.progress,
            total=event.total,
            elapsed_sec=event.elapsed_sec,
        )
        update = update_tool_call(
            event.tool_use_id, status=self._wire.status(ToolStatusName.IN_PROGRESS)
        )
        update.field_meta = {
            MetaKey.PROGRESS.value: meta.model_dump(mode="json", exclude_none=True)
        }

        return update

    def permission_request(
        self, request: CanUseToolRequest, hints: ToolHints | None
    ) -> PermissionAsk:
        tool_call = ToolCallUpdate(
            tool_call_id=request.tool_use_id,
            title=self._titles.title(request.tool_name, request.input),
            kind=self._wire.kind(self.tool_kind(hints)),
            status=self._wire.status(ToolStatusName.PENDING),
            raw_input=dict(request.input),
        )
        options: list[PermissionOption] = []
        for choice in PermissionChoice:
            options.append(
                PermissionOption(
                    option_id=choice.value,
                    name=choice.label,
                    kind=self._wire.option_kind(choice),
                )
            )

        return PermissionAsk(tool_call=tool_call, options=options)

    def permission_answer(
        self, response: RequestPermissionResponse, request: CanUseToolRequest
    ) -> PermissionAnswer:
        """Ответ клиента → ответ ядру; отмена и неизвестный вариант — отказ."""
        outcome = response.outcome
        if not isinstance(outcome, SelectedPermissionOutcome):
            return DenyAnswer()

        try:
            choice = PermissionChoice(outcome.option_id)
        except ValueError:
            logger.warning(
                "permission: the client chose unknown option %r for %s; treated "
                "as a rejection",
                outcome.option_id,
                request.tool_name,
            )
            return DenyAnswer()

        if choice is PermissionChoice.REJECT_ONCE:
            return DenyAnswer()

        if choice is PermissionChoice.ALLOW_ALWAYS:
            return AllowAnswer(updated_permissions=list(request.permission_suggestions))

        return AllowAnswer()


class PromptText:
    """Текст вопроса из блоков `session/prompt`: текст как есть, ссылка на
    ресурс — ссылкой markdown; прочие блоки агент не объявлял."""

    LINK: ClassVar[str] = "[@{name}]({uri})"

    def render(self, blocks: Sequence[PromptBlock]) -> str:
        """Ошибки:
        RequestError — блок неподдержанного вида или пустой вопрос (-32602).
        """
        parts: list[str] = []
        for block in blocks:
            parts.append(self._part(block))

        text = "".join(parts)
        if not text.strip():
            data = {ErrorDataKey.DETAILS.value: "the prompt has no text to send"}
            raise RequestError.invalid_params(data)

        return text

    def _part(self, block: PromptBlock) -> str:
        if isinstance(block, TextContentBlock):
            return block.text

        if isinstance(block, ResourceContentBlock):
            return self.LINK.format(name=block.name, uri=block.uri)

        details = (
            f"prompt blocks of type '{block.type}' are not supported: this agent "
            "declared no image, audio or embedded context capability; send text "
            "and resource_link blocks"
        )
        raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})


class McpServerSpecs:
    """Серверы MCP из `session/new` → записи формата `.mcp.json` настроек,
    из которых сборка делает конфиги клиента."""

    def specs(self, servers: Sequence[McpServerBlock]) -> dict[str, McpServerJson]:
        """Ошибки:
        RequestError — сервер неподдержанного вида или повтор имени (-32602).
        """
        specs: dict[str, McpServerJson] = {}
        for server in servers:
            if server.name in specs:
                details = (
                    f"mcpServers names '{server.name}' twice; names must be unique"
                )
                raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

            specs[server.name] = self._spec(server)

        return specs

    def _spec(self, server: McpServerBlock) -> McpServerJson:
        if isinstance(server, McpServerStdio):
            env: dict[str, str] = {}
            for variable in server.env:
                env[variable.name] = variable.value

            return McpServerJson(
                type=McpTransportJson.STDIO,
                command=server.command,
                args=list(server.args),
                env=env,
            )

        if isinstance(server, McpServerHttp):
            headers: dict[str, str] = {}
            for header in server.headers:
                headers[header.name] = header.value

            return McpServerJson(
                type=McpTransportJson.HTTP, url=server.url, headers=headers
            )

        details = (
            f"mcpServers entry '{server.name}' has type '{server.type}', which this "
            "agent does not support: it declared mcpCapabilities http only (stdio "
            "is always accepted)"
        )
        raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})


class ClientLink:
    """Вызовы к клиенту по соединению SDK: уведомление `session/update` и
    запрос `session/request_permission`. Создаёт сервер и отдаёт агенту."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    async def session_update(self, session_id: str, update: Update) -> None:
        await notify_model(
            self._connection,
            AcpMethod.SESSION_UPDATE.value,
            SessionNotification(session_id=session_id, update=update),
        )

    async def request_permission(
        self, session_id: str, ask: PermissionAsk
    ) -> RequestPermissionResponse:
        return await request_model(
            self._connection,
            AcpMethod.REQUEST_PERMISSION.value,
            RequestPermissionRequest(
                session_id=session_id,
                tool_call=ask.tool_call,
                options=list(ask.options),
            ),
            RequestPermissionResponse,
        )


class PromptWaiters:
    """Ожидания ответов на `session/prompt` по идентификаторам элементов
    очереди: итог хода закрывает ожидания всех элементов, которые он
    обслужил; очистка очереди отвечает им отменой; падение обслуживания —
    ошибкой."""

    def __init__(self, mapping: AcpMapping) -> None:
        self._mapping = mapping
        self._pending: dict[str, asyncio.Future[PromptResponse]] = {}

    def register(self, item_id: str) -> asyncio.Future[PromptResponse]:
        future: asyncio.Future[PromptResponse] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[item_id] = future

        return future

    def settle(self, result: ResultEvent) -> None:
        for item_id in result.prompt_ids:
            future = self._pending.pop(item_id, None)
            if future is None or future.done():
                continue

            try:
                future.set_result(self._mapping.prompt_response(result))
            except RequestError as exc:
                future.set_exception(exc)

    def cancel(self, item_ids: Sequence[str]) -> None:
        for item_id in item_ids:
            future = self._pending.pop(item_id, None)
            if future is None or future.done():
                continue

            future.set_result(self._mapping.cancelled_response())

    def fail(self, error: RequestError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

        self._pending.clear()


class SessionRelay(EventSink):
    """Реализация EventSink сессии ACP: события ядра → уведомления клиенту.

    Создаёт вход на каждую сессию до сборки агента и отдаёт ядру
    приёмником; агент привязывается после открытия (`attach`), потому что
    пометки инструментов и ответы на вопросы живут у него. Уведомления
    ждут отправки в порядке событий, так что итог хода приходит после них;
    вопрос `can_use_tool` уходит клиенту отдельной задачей, которую снимает
    отмена хода. После ухода клиента уведомления некому слать: приёмник
    отвязывается и дальше молчит.
    """

    def __init__(
        self,
        session_id: str,
        link: ClientLink,
        mapping: AcpMapping,
        waiters: PromptWaiters,
    ) -> None:
        self._session_id = session_id
        self._link = link
        self._mapping = mapping
        self._waiters = waiters
        self._agent: Agent | None = None
        self._asks: dict[str, asyncio.Task[None]] = {}
        self._detached = False

    def attach(self, agent: Agent) -> None:
        self._agent = agent

    async def emit(self, event: AgentEvent) -> None:
        updates = self._updates(event)
        if updates is not None:
            await self._send_all(updates)
            return

        self._control(event)

    def _updates(self, event: AgentEvent) -> Sequence[Update] | None:
        """Уведомления по событию; None — событие не про ленту."""
        if isinstance(event, StreamEvent):
            return self._mapping.chunks(event)

        if isinstance(event, RecordEvent):
            return self._record_updates(event.record)

        if isinstance(event, ToolStartedEvent):
            return [self._mapping.tool_started(event)]

        if isinstance(event, ToolProgressEvent):
            return [self._mapping.tool_progress(event)]

        return None

    def _control(self, event: AgentEvent) -> None:
        if isinstance(event, ResultEvent):
            self._waiters.settle(event)
            return

        if isinstance(event, ControlRequestEvent):
            self._ask(event)
            return

        if isinstance(event, ControlCancelEvent):
            self.withdraw(event.request_id)
            return

        if isinstance(event, ErrorEvent):
            logger.warning("session %s: %s", self._session_id, event.message)
            return

        if isinstance(event, InitEvent):
            logger.info(
                "session %s: opened with model %s and %d tool(s)",
                event.session_id,
                event.model,
                len(event.tools),
            )

    def _record_updates(self, record: Record) -> Sequence[Update]:
        if isinstance(record, AssistantRecord):
            updates: list[Update] = []
            for block in record.message.tool_uses():
                updates.append(self._mapping.tool_call(block, self._hints(block.name)))

            return updates

        if isinstance(record, UserRecord):
            return self._mapping.tool_results(record)

        return ()

    def _hints(self, name: str) -> ToolHints | None:
        if self._agent is None:
            return None

        return self._agent.tool_hints(name)

    async def _send_all(self, updates: Sequence[Update]) -> None:
        for update in updates:
            await self._send(update)

    async def _send(self, update: Update) -> None:
        if self._detached:
            return

        try:
            await self._link.session_update(self._session_id, update)
        except ConnectionError as exc:
            self._detached = True
            logger.warning(
                "session %s: the client connection is closed (%s); further "
                "updates of this session are dropped",
                self._session_id,
                exc,
            )

    def _ask(self, event: ControlRequestEvent) -> None:
        task = asyncio.create_task(
            self._permission(event), name=f"acp-permission-{event.request_id}"
        )
        self._asks[event.request_id] = task

    async def _permission(self, event: ControlRequestEvent) -> None:
        agent = self._agent
        if agent is None:
            msg = "A permission question arrived before the agent was attached."
            raise RuntimeError(msg)

        request = event.request
        ask = self._mapping.permission_request(request, self._hints(request.tool_name))
        try:
            response = await self._link.request_permission(self._session_id, ask)
            answer = self._mapping.permission_answer(response, request)
        except ConnectionError as exc:
            logger.warning(
                "session %s: permission for %s cannot be asked, the client is gone "
                "(%s); the call is rejected",
                self._session_id,
                request.tool_name,
                exc,
            )
            answer = DenyAnswer()
        except RequestError as exc:
            logger.warning(
                "session %s: the client answered the permission request for %s "
                "with an error (%s); the call is rejected",
                self._session_id,
                request.tool_name,
                exc,
            )
            answer = DenyAnswer()
        finally:
            self._asks.pop(event.request_id, None)

        taken = agent.answer(event.request_id, answer)
        if not taken:
            logger.info(
                "session %s: the answer to question %s came after the turn moved on",
                self._session_id,
                event.request_id,
            )

    def withdraw(self, request_id: str) -> None:
        """Вопрос снят ядром: ответ клиента больше не нужен."""
        task = self._asks.pop(request_id, None)
        if task is not None:
            task.cancel()

    def withdraw_all(self) -> None:
        for request_id in list(self._asks):
            self.withdraw(request_id)


class OpenedSession(Protocol):
    """Открытая сессия сборки: агент ядра и всё, что надо закрыть вместе с ним."""

    @property
    @abstractmethod
    def agent(self) -> Agent: ...

    @abstractmethod
    async def close(self) -> None:
        """Гасит ход, сбрасывает журнал, закрывает серверы MCP сессии."""


class SessionAssembly(Protocol):
    """Сборка сессии из настроек процесса и параметров `session/new`.

    Реализует вход процесса: он знает профиль модели, хранилище истории и
    клиент MCP; протокольный слой знает только события и ответы.
    """

    @abstractmethod
    async def open(
        self,
        session_id: SessionId,
        cwd: Path,
        servers: Mapping[str, McpServerJson],
        sink: EventSink,
    ) -> OpenedSession:
        """Ошибки:
        SessionRefusedError — параметры сессии не годятся (сервер MCP с
            неподдержанными заголовками).
        SessionOpenError — сессию открыть не удалось.
        """


class AcpSession:
    """Одна сессия ACP: открытый агент, его обслуживание очереди, приёмник и
    ожидания ответов на `prompt`."""

    def __init__(
        self,
        session_id: str,
        opened: OpenedSession,
        relay: SessionRelay,
        waiters: PromptWaiters,
    ) -> None:
        self._session_id = session_id
        self._opened = opened
        self._relay = relay
        self._waiters = waiters
        self._serving = asyncio.create_task(
            opened.agent.serve(), name=f"acp-serve-{session_id}"
        )
        self._serving.add_done_callback(self._served)

    @property
    def agent(self) -> Agent:
        return self._opened.agent

    def _served(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return

        failure = task.exception()
        if failure is None:
            return

        logger.error(
            "session %s: the agent stopped serving: %s", self._session_id, failure
        )
        data = {
            ErrorDataKey.KIND.value: type(failure).__name__,
            ErrorDataKey.MESSAGE.value: str(failure),
        }
        self._waiters.fail(RequestError.internal_error(data))

    async def prompt(self, text: str) -> PromptResponse:
        """Ставит вопрос в очередь ядра и ждёт итога хода, который его обслужит.

        Ошибки:
        RequestError — очередь не приняла элемент (-32602), обслуживание
            сессии остановилось (-32603), модель отказала (-32603).
        """
        if self._serving.done():
            details = (
                f"session {self._session_id} no longer serves prompts: its agent "
                "stopped; open a new session"
            )
            raise RequestError.internal_error({ErrorDataKey.DETAILS.value: details})

        agent = self.agent
        priority: QueuePriority | None = None
        if agent.session.state is not SessionState.IDLE:
            priority = QueuePriority.LATER

        if len(agent.queue) > 0:
            priority = QueuePriority.LATER

        entry = QueueEntry(kind=QueueKind.PROMPT, text=text, priority=priority)
        try:
            item = agent.queue.enqueue(entry)
        except QueueRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        return await self._waiters.register(item.id)

    def cancel(self, reason: CoreStopReason) -> None:
        """Прерывает ход и очищает очередь; ожидающие получают `cancelled`."""
        queued: list[str] = []
        for item in self.agent.queue.items():
            queued.append(item.id)

        self.agent.interrupt(reason, cancel_queued=True)
        self._waiters.cancel(queued)

    async def close(self) -> None:
        self._relay.withdraw_all()
        await self._opened.close()
        try:
            await self._serving
        except Exception as exc:
            logger.error(
                "session %s: serving ended with an error: %s", self._session_id, exc
            )


class PromptRequests:
    """Наблюдатель кадров соединения: под каким `id` идёт какой запрос
    `session/prompt`, чтобы `$/cancel_request` нашёл сессию. Регистрируется
    у соединения SDK; ответ агента снимает запись."""

    def __init__(self) -> None:
        self._sessions: dict[str, str] = {}

    def __call__(self, event: FrameEvent) -> None:
        try:
            frame = Frame.model_validate(event.message)
        except ValidationError as exc:
            logger.debug("frame observer: unreadable frame skipped: %s", exc)
            return

        if frame.id is None:
            return

        key = str(frame.id)
        if event.direction is StreamDirection.OUTGOING:
            if frame.method is None:
                self._sessions.pop(key, None)

            return

        if frame.method != AcpMethod.SESSION_PROMPT.value:
            return

        if frame.params is None:
            return

        try:
            params = SessionParams.model_validate(frame.params)
        except ValidationError:
            return

        self._sessions[key] = params.session_id

    def session_of(self, request_id: int | str) -> str | None:
        return self._sessions.get(str(request_id))


class AcpAgent(AcpAgentProtocol):
    """Реализация Agent SDK над сессиями ядра.

    Создаёт вход процесса со сборкой сессий; сервер (AcpServer) привязывает
    канал к клиенту. Один объект обслуживает все сессии соединения: каждая
    — свой агент ядра с серверами MCP из `session/new`.
    """

    NAME: ClassVar[str] = "boba"

    def __init__(self, assembly: SessionAssembly, version: str) -> None:
        self._assembly = assembly
        self._version = version
        self._link: ClientLink | None = None
        self._sessions: dict[str, AcpSession] = {}
        self._mapping = AcpMapping()
        self._prompts = PromptText()
        self._specs = McpServerSpecs()
        self._ids = IdMint()
        self._requests = PromptRequests()
        self._client: ClientCapabilities | None = None

    @property
    def requests(self) -> PromptRequests:
        return self._requests

    @property
    def client_capabilities(self) -> ClientCapabilities | None:
        """Возможности клиента из `initialize`; None — рукопожатия не было."""
        return self._client

    def attach(self, link: ClientLink) -> None:
        self._link = link

    def _linked(self) -> ClientLink:
        if self._link is None:
            msg = "The ACP agent has no client connection yet; attach() comes first."
            raise RuntimeError(msg)

        return self._link

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        self._client = client_capabilities
        name = "unknown client"
        if client_info is not None:
            name = f"{client_info.name} {client_info.version}"

        logger.info("initialize: %s speaks protocol version %d", name, protocol_version)

        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=False, audio=False, embedded_context=False
                ),
                mcp_capabilities=McpCapabilities(http=True, sse=False),
                session_capabilities=SessionCapabilities(
                    list=SessionListCapabilities(),
                    resume=SessionResumeCapabilities(),
                    close=SessionCloseCapabilities(),
                    delete=SessionDeleteCapabilities(),
                    fork=SessionForkCapabilities(),
                ),
            ),
            agent_info=Implementation(name=self.NAME, version=self._version),
            auth_methods=[],
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        """Ошибки:
        RequestError — `cwd` не абсолютный или серверы MCP не годятся
            (-32602); сессию открыть не удалось (-32603).
        """
        directory = Path(cwd)
        if not directory.is_absolute():
            details = f"cwd must be an absolute path, got '{cwd}'"
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        servers: list[McpServerBlock] = []
        if mcp_servers is not None:
            servers = mcp_servers

        specs = self._specs.specs(servers)
        session_id = self._ids.uuid()
        waiters = PromptWaiters(self._mapping)
        relay = SessionRelay(session_id, self._linked(), self._mapping, waiters)
        try:
            opened = await self._assembly.open(
                SessionId(value=session_id), directory, specs, relay
            )
        except SessionRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc
        except SessionOpenError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        relay.attach(opened.agent)
        self._sessions[session_id] = AcpSession(session_id, opened, relay, waiters)
        logger.info(
            "session %s: new, cwd %s, %d MCP server(s)", session_id, cwd, len(specs)
        )

        return NewSessionResponse(session_id=session_id)

    async def prompt(
        self, session_id: str, prompt: list[PromptBlock], **kwargs: Any
    ) -> PromptResponse:
        """Ошибки:
        RequestError — сессия неизвестна (-32002), блоки не годятся (-32602),
            модель отказала (-32603).
        """
        session = self._session(session_id)
        text = self._prompts.render(prompt)

        return await session.prompt(text)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            logger.info("cancel: session %s is unknown; nothing to cancel", session_id)
            return

        session.cancel(CoreStopReason.USER_STOP)

    async def cancel_request(self, params: Any) -> None:
        """Маршрут `$/cancel_request`: отмена запроса `session/prompt` по его
        `requestId` — как `session/cancel` его сессии; прочие запросы
        исполняются сразу, их отменять нечего."""
        try:
            notification = CancelRequestNotification.model_validate(params)
        except ValidationError as exc:
            logger.warning("$/cancel_request with unreadable params ignored: %s", exc)
            return

        if notification.request_id is None:
            return

        session_id = self._requests.session_of(notification.request_id)
        if session_id is None:
            logger.info(
                "$/cancel_request %s matches no running session/prompt; ignored",
                notification.request_id,
            )
            return

        await self.cancel(session_id)

    def _session(self, session_id: str) -> AcpSession:
        """Ошибки:
        RequestError — сессия этому процессу неизвестна (-32002).
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise RequestError(
                RequestError.resource_not_found().code,
                f"session {session_id} is unknown to this agent process",
                {ErrorDataKey.SESSION_ID.value: session_id},
            )

        return session

    def shutdown(self, reason: CoreStopReason) -> None:
        """Прерывает ходы всех сессий: процесс останавливается."""
        for session in self._sessions.values():
            session.cancel(reason)

    async def close_all(self) -> None:
        for session_id, session in list(self._sessions.items()):
            await session.close()
            self._sessions.pop(session_id, None)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[McpServerBlock] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse | None:
        raise RequestError.method_not_found(AcpMethod.SESSION_LOAD.value)

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> ListSessionsResponse:
        raise RequestError.method_not_found(AcpMethod.SESSION_LIST.value)

    async def set_session_mode(
        self, session_id: str, mode_id: str, **kwargs: Any
    ) -> SetSessionModeResponse | None:
        raise RequestError.method_not_found(AcpMethod.SESSION_SET_MODE.value)

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse | None:
        raise RequestError.method_not_found(AcpMethod.SESSION_SET_CONFIG_OPTION.value)

    async def authenticate(
        self, method_id: str, **kwargs: Any
    ) -> AuthenticateResponse | None:
        raise RequestError.method_not_found(AcpMethod.AUTHENTICATE.value)

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        **kwargs: Any,
    ) -> ForkSessionResponse:
        raise RequestError.method_not_found(AcpMethod.SESSION_FORK.value)

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        raise RequestError.method_not_found(AcpMethod.SESSION_RESUME.value)

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> CloseSessionResponse | None:
        raise RequestError.method_not_found(AcpMethod.SESSION_CLOSE.value)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(f"_{method}")

    def on_connect(self, conn: AcpClientProtocol) -> None:
        """Соединение SDK здесь не используется: канал к клиенту даёт
        AcpServer через `attach`."""

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        logger.info("extension notification _%s is not supported; ignored", method)


class AcpServer:
    """Соединение ACP по потокам: маршрутизатор SDK с включёнными
    нестабильными методами плюс маршрут `$/cancel_request`, которого у
    маршрутизатора нет. Создаёт вход процесса; `serve` живёт до конца ввода
    или до `stop`, затем закрывает сессии."""

    def __init__(
        self,
        agent: AcpAgent,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._agent = agent
        router = build_agent_router(agent, use_unstable_protocol=True)
        router.add_route(
            Route(
                method=AcpMethod.CANCEL_REQUEST.value,
                func=agent.cancel_request,
                kind="notification",
            )
        )
        self._connection = Connection(
            router, writer, reader, observers=[agent.requests], listening=False
        )
        agent.attach(ClientLink(self._connection))
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        """Остановка снаружи (сигнал): обслуживание кончается, сессии закрываются."""
        self._stopped.set()

    async def serve(self) -> None:
        receiving = asyncio.create_task(
            self._connection.main_loop(), name="acp-receive"
        )
        stopping = asyncio.create_task(self._stopped.wait(), name="acp-stop")
        try:
            await asyncio.wait(
                {receiving, stopping}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stopping.cancel()
            receiving.cancel()
            await asyncio.gather(receiving, stopping, return_exceptions=True)
            await self._agent.close_all()
            await self._connection.close()
