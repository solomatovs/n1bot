"""Процесс `boba acp` на стенде клиентом ACP v2 (ClientConnection пакета
boba-transport-acp): модель — FakeLlmApp, инструменты — регистрация
MCP-over-ACP на стороне клиента (StandRegistration), которая отвечает на
mcp/message сама, без библиотеки MCP. Сценарии: рукопожатие с
`capabilities.session.mcp.acp`; ход: ответ `messageId`, эхо вопроса,
`state_update` running/idle, сбой модели после вставки — idle с ошибкой;
список инструментов страницами до ответа `session/new`; вызов с ходом
выполнения `tool_call_update`; остановка посреди вызова отменяет запрос
mcp/message; внутренняя и внешняя ошибки MCP — разные исходы;
`input_required`; блоки только для человека не попадают модели; две сессии на
одной регистрации; разрешение с запоминанием правила и отказ; второй `prompt`
в очереди; отказы на чужие блоки, серверы не `acp`, неизвестную сессию и
относительный `cwd`; секреты только в файле 0600; stdout — только кадры
JSON-RPC; SIGTERM и конец ввода завершают процесс с кодом 0 и сброшенным
журналом. История: `session/resume` с `replayFrom: start` второго процесса
воспроизводит беседу теми же уведомлениями, что живой ход (один рендерер
FeedView), до ответа; без `replayFrom` — без воспроизведения;
`session/close`; `session/list` страницами с курсором и фильтром `cwd`;
`session/delete` повторно — успех. Настройки сессии: `configOptions` в
ответах, `set_config_option` с уведомлением, смена профиля модели между
ходами и отказ при идущем ходе. Служебные уведомления: `usage_update`,
`compaction_update`, `session_info_update`. Ветвление: `session/fork` с
`boba/upToMessageId`, без ключа, срез на первом вопросе, отказы."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, TypeVar

import mcp_types as mt
import pytest
import uvicorn
from acp.connection import StreamDirection
from acp.exceptions import RequestError
from acp.experimental.v2 import Client, schema
from mcp_types.jsonrpc import ErrorData
from mcp_types.methods import parse_client_request
from pydantic import AnyUrl, BaseModel, ConfigDict

from boba.cli.acp_server import ConfigOptionId, PermissionChoice, ToolKindName
from boba.stand.acp_frames import Frames
from boba.stand.ui.fake_llm import FailureName, FakeLlmApp, ScenarioName
from boba.stand.ui.stand import StandPorts
from boba.transport.acp.protocol import (
    AcpErrorCode,
    AcpVersion,
    ClientConnection,
    IdleErrorUpdate,
    McpErrorOutcome,
    McpMessage,
    McpOutcome,
    McpResultOutcome,
    McpVersion,
    MetaKey,
    StopReason,
    ToolStatus,
)

pytestmark = pytest.mark.anyio

READ_TIMEOUT_SEC = 30.0
API_KEY = "sk-acp-test-secret-key"
OWNER_ONLY = 0o600
Document = dict[str, Any]
U = TypeVar("U")


def call(*specs: dict[str, object]) -> list[schema.TextContentBlock]:
    """Вопрос сценария CALL: модель вызывает перечисленные инструменты."""
    text = f"{ScenarioName.CALL.value} {json.dumps(list(specs))}"
    if len(specs) == 1:
        text = f"{ScenarioName.CALL.value} {json.dumps(specs[0])}"

    return [schema.TextContentBlock(text=text)]


class FakeModel:
    """FakeLlmApp под uvicorn в цикле теста; `estimate_usage` — учёт токенов
    по знакам, как у настоящего провайдера, нужен сценариям сжатия;
    `unmarked` — сценарий для вопроса без маркера (чужие клиенты и TCK шлют
    обычный текст); `token_delay_sec` — пауза между кусками выдачи."""

    def __init__(
        self,
        estimate_usage: bool = False,
        unmarked: ScenarioName | None = None,
        token_delay_sec: float = 0.0,
    ) -> None:
        self.app = FakeLlmApp(
            token_delay_sec=token_delay_sec,
            estimate_usage=estimate_usage,
            unmarked=unmarked,
        )
        self.port = StandPorts().free()
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app.asgi(),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                timeout_graceful_shutdown=1,
            )
        )
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


class RegisteredTool(StrEnum):
    """Инструменты StandRegistration; имена — как у standard_server.py, где
    поведение то же."""

    ADD = "add"
    SHOUT = "shout"
    SLOW = "progress"
    FAILING = "broken"
    GONE = "gone"
    RETIRED = "retired"
    ASKING = "asking"
    CHOOSING = "choosing"
    MIXED = "blocks"
    LARGE = "large"


class StandRegistration:
    """Регистрация серверов на стороне тестового клиента в роли чата aid:
    отвечает на mcp/message сама моделями mcp_types. `tools/list` отдаёт
    страницами по PAGE, `tools/call` ведёт себя по RegisteredTool: ADD
    складывает, SHOUT кричит, SLOW шлёт ход `notifications/progress` через
    `connection` и ждёт, FAILING отвечает isError, GONE — внешней ошибкой
    ACP -33002, RETIRED — внутренней ошибкой MCP -32602 (инструмент снят после
    листинга), ASKING — `input_required`, MIXED — блоки для человека и для
    модели, LARGE — текст заданной длины. serverId вне `servers` — внешняя
    ошибка -33001. Копит запросы и requestId отменённых ответов."""

    STAND: ClassVar[schema.AcpMcpServer] = schema.AcpMcpServer(
        name="stand", server_id="stand:3f9c2a"
    )
    PAGE: ClassVar[int] = 3
    JOURNAL: ClassVar[dict[str, Any]] = {
        "com.boba/journal": ["boba-journal://run/call/stdout"]
    }
    """_meta уведомления SLOW о журнале вызова: агент передаёт его как есть."""
    TOOLS: ClassVar[Sequence[mt.Tool]] = (
        mt.Tool(
            name=RegisteredTool.ADD,
            description="Adds two numbers.",
            input_schema={
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            },
            annotations=mt.ToolAnnotations(read_only_hint=True),
        ),
        mt.Tool(
            name=RegisteredTool.SHOUT,
            description="Returns the text in upper case.",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            annotations=mt.ToolAnnotations(
                read_only_hint=False, destructive_hint=False
            ),
        ),
        mt.Tool(
            name=RegisteredTool.SLOW,
            description="Reports progress once per step over the given seconds.",
            input_schema={
                "type": "object",
                "properties": {
                    "steps": {"type": "integer"},
                    "seconds": {"type": "number"},
                },
            },
            annotations=mt.ToolAnnotations(read_only_hint=True),
        ),
        mt.Tool(
            name=RegisteredTool.FAILING,
            description="Always fails.",
            input_schema={"type": "object"},
        ),
        mt.Tool(
            name=RegisteredTool.GONE,
            description="Its server is unreachable.",
            input_schema={"type": "object"},
        ),
        mt.Tool(
            name=RegisteredTool.RETIRED,
            description="Is removed from the server after the listing.",
            input_schema={"type": "object"},
        ),
        mt.Tool(
            name=RegisteredTool.ASKING,
            description="Asks for more input.",
            input_schema={"type": "object"},
        ),
        mt.Tool(
            name=RegisteredTool.CHOOSING,
            description="Asks the user for the target table, then answers.",
            input_schema={"type": "object"},
        ),
        mt.Tool(
            name=RegisteredTool.MIXED,
            description="Answers with blocks for the user and the model.",
            input_schema={"type": "object"},
            annotations=mt.ToolAnnotations(read_only_hint=True),
        ),
        mt.Tool(
            name=RegisteredTool.LARGE,
            description="Returns a text of exactly the given size.",
            input_schema={
                "type": "object",
                "properties": {"size": {"type": "integer"}},
            },
            annotations=mt.ToolAnnotations(read_only_hint=True),
        ),
    )
    FAILURE: ClassVar[str] = "no such table: orders"
    CHOOSE_STATE: ClassVar[str] = "choosing-round-1"
    CHOOSE_FORM: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "title": "Target",
                "description": "Which table should receive the rows?",
                "enum": ["sales.orders", "sales.orders_stage"],
                "enumNames": ["Orders", "Orders stage"],
            }
        },
        "required": ["table"],
    }
    WIRE: ClassVar[Mapping[str, Any]] = {
        "mode": "json",
        "by_alias": True,
        "exclude_none": True,
    }

    def __init__(self, servers: Sequence[schema.AcpMcpServer]) -> None:
        self._served = {server.server_id for server in servers}
        self.connection: ClientConnection | None = None
        self.received: list[McpMessage] = []
        self.cancelled: list[str] = []

    async def answer(self, message: McpMessage) -> McpOutcome:
        self.received.append(message)
        if message.server_id not in self._served:
            msg = f"No MCP server is registered under serverId {message.server_id!r}"
            raise RequestError(AcpErrorCode.UNKNOWN_SERVER, msg)

        request = parse_client_request(
            message.method, McpVersion.V2026_07_28, message.params
        )
        if isinstance(request, mt.DiscoverRequest):
            capabilities = mt.ServerCapabilities(tools=mt.ToolsCapability())
            discovered = mt.DiscoverResult(
                supported_versions=[McpVersion.V2026_07_28.value],
                capabilities=capabilities,
            )
            return McpResultOutcome(result=discovered.model_dump(**self.WIRE))

        if isinstance(request, mt.ListToolsRequest):
            start = 0
            if request.params is not None and request.params.cursor is not None:
                start = int(request.params.cursor)

            end = start + self.PAGE
            page = mt.ListToolsResult(tools=list(self.TOOLS[start:end]))
            if end < len(self.TOOLS):
                page = mt.ListToolsResult(
                    tools=list(self.TOOLS[start:end]), next_cursor=str(end)
                )

            return McpResultOutcome(result=page.model_dump(**self.WIRE))

        if not isinstance(request, mt.CallToolRequest):
            missing = f"Method not found: {request.method}"
            return McpErrorOutcome(error=ErrorData(code=-32601, message=missing))

        return await self._call(message, request)

    async def _call(
        self, message: McpMessage, request: mt.CallToolRequest
    ) -> McpOutcome:
        """Исход `tools/call` по RegisteredTool."""
        name = request.params.name
        arguments: Mapping[str, Any] = {}
        if request.params.arguments is not None:
            arguments = request.params.arguments

        reply: mt.CallToolResult | mt.InputRequiredResult
        match name:
            case RegisteredTool.ADD:
                total = int(arguments["a"]) + int(arguments["b"])
                reply = mt.CallToolResult(content=[mt.TextContent(text=str(total))])
            case RegisteredTool.SHOUT:
                shout = str(arguments["text"]).upper()
                reply = mt.CallToolResult(content=[mt.TextContent(text=shout)])
            case RegisteredTool.SLOW:
                reply = await self._progress(message, request, arguments)
            case RegisteredTool.FAILING:
                failure = mt.TextContent(text=self.FAILURE)
                reply = mt.CallToolResult(content=[failure], is_error=True)
            case RegisteredTool.GONE:
                gone = "The MCP server of 'gone' is unreachable"
                raise RequestError(AcpErrorCode.BACKEND_FAILED, gone)
            case RegisteredTool.ASKING:
                reply = mt.InputRequiredResult(request_state="opaque")
            case RegisteredTool.CHOOSING:
                reply = self._choose(request.params)
            case RegisteredTool.MIXED:
                reply = mt.CallToolResult(
                    content=[
                        mt.TextContent(
                            text="for the user",
                            annotations=mt.Annotations(audience=["user"]),
                        ),
                        mt.TextContent(
                            text="for the model",
                            annotations=mt.Annotations(audience=["user", "assistant"]),
                        ),
                        mt.ResourceLink(
                            name="report.csv",
                            uri="file:///report.csv",
                            mime_type="text/csv",
                        ),
                    ]
                )
            case RegisteredTool.LARGE:
                size = int(arguments["size"])
                large = ("0123456789" * (size // 10 + 1))[:size]
                reply = mt.CallToolResult(content=[mt.TextContent(text=large)])
            case _:
                unknown = ErrorData(
                    code=-32602, message=f"Unknown tool: {name}", data={"tool": name}
                )
                return McpErrorOutcome(error=unknown)

        return McpResultOutcome(result=reply.model_dump(**self.WIRE))

    def _choose(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolResult | mt.InputRequiredResult:
        """CHOOSING: первый запрос — форма выбора таблицы (с устаревшими
        enumNames), повтор с ответом и тем же requestState — выбранная таблица
        либо отказ пользователя."""
        responses = params.input_responses
        if responses is None:
            form = mt.ElicitRequestFormParams(
                message="choosing needs the target table before it can start",
                requested_schema=self.CHOOSE_FORM,
            )
            return mt.InputRequiredResult(
                input_requests={"target": mt.ElicitRequest(params=form)},
                request_state=self.CHOOSE_STATE,
            )

        assert params.request_state == self.CHOOSE_STATE
        answer = responses["target"]
        assert isinstance(answer, mt.ElicitResult)
        if answer.action != "accept":
            told = mt.TextContent(text=f"the user answered {answer.action}")
            return mt.CallToolResult(content=[told], is_error=True)

        assert answer.content is not None
        chosen = mt.TextContent(text=f"rows go to {answer.content['table']}")

        return mt.CallToolResult(content=[chosen])

    async def _progress(
        self,
        message: McpMessage,
        request: mt.CallToolRequest,
        arguments: Mapping[str, Any],
    ) -> mt.CallToolResult:
        """SLOW: уведомление о журнале вызова (без текста и итога, ссылка в
        `_meta`), затем ход `notifications/progress` по шагу и пауза; отмену
        запоминает."""
        steps = int(arguments["steps"])
        pause = float(arguments["seconds"]) / steps
        meta = request.params.meta
        assert meta is not None
        token = meta.get("progress_token")
        assert token is not None
        connection = self.connection
        assert connection is not None
        journal = mt.ProgressNotification(
            params=mt.ProgressNotificationParams(
                progress_token=token, progress=0, _meta=self.JOURNAL
            )
        )
        await connection.notify(
            McpMessage(
                server_id=message.server_id,
                request_id=message.request_id,
                **journal.model_dump(**self.WIRE),
            )
        )
        try:
            for step in range(1, steps + 1):
                progress = mt.ProgressNotification(
                    params=mt.ProgressNotificationParams(
                        progress_token=token,
                        progress=step,
                        total=steps,
                        message=f"step {step} of {steps}",
                    )
                )
                await connection.notify(
                    McpMessage(
                        server_id=message.server_id,
                        request_id=message.request_id,
                        **progress.model_dump(**self.WIRE),
                    )
                )
                await asyncio.sleep(pause)
        except asyncio.CancelledError:
            self.cancelled.append(message.request_id)
            raise

        done = f"did {steps} steps"

        return mt.CallToolResult(content=[mt.TextContent(text=done)])


class AskedForm(BaseModel):
    """Запрос `elicitation/create`, каким его получил клиент."""

    model_config = ConfigDict(frozen=True)

    message: str
    session_id: str | None
    tool_call_id: str | None
    requested: schema.ElicitationSchema
    meta: Mapping[str, Any]


class AcpClient(Client):
    """Тестовый клиент ACP v2 в роли чата aid: копит уведомления по сессиям
    (в том числе idle с ошибкой), отвечает на запрос разрешения выбранным
    вариантом либо отменой; `hold` задерживает ответ. Форму
    `elicitation/create` запоминает и отвечает `reply`; `hold_form`
    задерживает ответ, снятый запрос считается в `withdrawn`."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []
        self.permissions: list[schema.ToolCallUpdate] = []
        self.options: list[Sequence[schema.PermissionOption]] = []
        self.choice: PermissionChoice | None = PermissionChoice.ALLOW_ONCE
        self.hold: asyncio.Event | None = None
        self.forms: list[AskedForm] = []
        self.replies: list[
            schema.AcceptElicitationResponse
            | schema.DeclineElicitationResponse
            | schema.CancelElicitationResponse
        ] = []
        self.hold_form: asyncio.Event | None = None
        self.withdrawn = 0
        self._changed = asyncio.Event()
        self._idles: dict[str, int] = {}

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))
        self._changed.set()

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
        assert isinstance(subject, schema.ToolCallPermissionSubjectVariant)
        self.permissions.append(subject.tool_call)
        self.options.append(options)
        self._changed.set()
        if self.hold is not None:
            await self.hold.wait()

        if self.choice is None:
            return schema.RequestPermissionResponse(
                outcome=schema.CancelledPermissionOutcome()
            )

        return schema.RequestPermissionResponse(
            outcome=schema.SelectedPermissionOutcomeVariant(option_id=self.choice.value)
        )

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
    ) -> (
        schema.AcceptElicitationResponse
        | schema.DeclineElicitationResponse
        | schema.CancelElicitationResponse
    ):
        assert mode == "form"
        assert requested_schema is not None
        self.forms.append(
            AskedForm(
                message=message,
                session_id=session_id,
                tool_call_id=tool_call_id,
                requested=requested_schema,
                meta=kwargs,
            )
        )
        self._changed.set()
        try:
            if self.hold_form is not None:
                await self.hold_form.wait()
        except asyncio.CancelledError:
            self.withdrawn += 1
            raise

        return self.replies.pop(0)

    # SDK считает метод протокола неподдержанным
    connect_mcp = Client.connect_mcp
    mcp_message = Client.mcp_message
    notify_mcp = Client.notify_mcp
    disconnect_mcp = Client.disconnect_mcp
    complete_elicitation = Client.complete_elicitation

    async def next_form(self, seen: int) -> AskedForm:
        while len(self.forms) <= seen:
            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), READ_TIMEOUT_SEC)

        return self.forms[seen]

    async def next_permission(self, seen: int) -> schema.ToolCallUpdate:
        while len(self.permissions) <= seen:
            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), READ_TIMEOUT_SEC)

        return self.permissions[seen]

    async def idle(
        self, session_id: str
    ) -> schema.IdleSessionStateUpdate | IdleErrorUpdate:
        """Следующий ещё не взятый `idle` сессии: конец её очередного хода."""
        taken = self._idles.get(session_id, 0)
        while True:
            found: list[schema.IdleSessionStateUpdate] = []
            for update in self.updates_of(session_id):
                if isinstance(update, schema.IdleSessionStateUpdate):
                    found.append(update)

            if len(found) > taken:
                self._idles[session_id] = taken + 1
                return found[taken]

            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), READ_TIMEOUT_SEC)

    def calls_of(self, tool_call_id: str) -> list[schema.SessionToolCallUpdate]:
        found: list[schema.SessionToolCallUpdate] = []
        for update in self.of(schema.SessionToolCallUpdate):
            if update.tool_call_id == tool_call_id:
                found.append(update)

        return found

    def updates_of(self, session_id: str) -> list[Any]:
        found: list[Any] = []
        for seen, update in self.updates:
            if seen == session_id:
                found.append(update)

        return found

    def of(self, kind: type[U]) -> list[U]:
        found: list[U] = []
        for _, update in self.updates:
            if isinstance(update, kind):
                found.append(update)

        return found


class FeedView:
    """Лента по уведомлениям сессии — один рендерер для живого хода и для
    воспроизведения: куски одного сообщения и целые сообщения склеиваются, у
    вызова остаются имя, заголовок, вид, аргументы, конечный статус, текст и
    `rawOutput`; состояние хода и служебные уведомления в ленту не идут."""

    def render(self, updates: Sequence[Any]) -> list[tuple[Any, ...]]:
        items: list[tuple[Any, ...]] = []
        calls: dict[str, dict[str, Any]] = {}
        for update in updates:
            if isinstance(update, schema.UserMessageUpdate):
                items.append(("user", update.message_id, self._text_of(update)))
                continue

            if isinstance(update, schema.AgentThoughtChunk | schema.AgentThoughtUpdate):
                self._glue(items, "thought", update)
                continue

            if isinstance(update, schema.AgentMessageChunk | schema.AgentMessageUpdate):
                self._glue(items, "agent", update)
                continue

            if not isinstance(update, schema.SessionToolCallUpdate):
                continue

            state = calls.get(update.tool_call_id)
            if state is None:
                state = {
                    "id": update.tool_call_id,
                    "name": update.name,
                    "title": update.title,
                    "kind": update.kind,
                    "raw_input": update.raw_input,
                    "status": update.status,
                    "text": None,
                    "raw_output": None,
                }
                calls[update.tool_call_id] = state
                items.append(("tool", state))
                continue

            state["status"] = update.status
            if update.content:
                state["text"] = result_text(update)

            if update.raw_output is not None:
                state["raw_output"] = update.raw_output

        return [self._frozen(item) for item in items]

    def without_user(
        self, rendered: Sequence[tuple[Any, ...]]
    ) -> list[tuple[Any, ...]]:
        return [item for item in rendered if item[0] != "user"]

    def user_texts(self, rendered: Sequence[tuple[Any, ...]]) -> list[str]:
        return [item[2] for item in rendered if item[0] == "user"]

    @staticmethod
    def _text_of(
        update: schema.UserMessageUpdate
        | schema.AgentMessageUpdate
        | schema.AgentThoughtUpdate
        | schema.AgentMessageChunk
        | schema.AgentThoughtChunk,
    ) -> str:
        """Текст куска или целого сообщения: у целого блоки склеиваются."""
        blocks: list[Any] = [update.content]
        if isinstance(update.content, list):
            blocks = list(update.content)

        parts: list[str] = []
        for block in blocks:
            assert isinstance(block, schema.TextContentBlock)
            parts.append(block.text)

        return "".join(parts)

    def _glue(
        self,
        items: list[tuple[Any, ...]],
        kind: str,
        update: schema.AgentMessageChunk
        | schema.AgentThoughtChunk
        | schema.AgentMessageUpdate
        | schema.AgentThoughtUpdate,
    ) -> None:
        chunk = self._text_of(update)
        if items and items[-1][0] == kind and items[-1][1] == update.message_id:
            items[-1] = (kind, update.message_id, items[-1][2] + chunk)
            return

        items.append((kind, update.message_id, chunk))

    @staticmethod
    def _frozen(item: tuple[Any, ...]) -> tuple[Any, ...]:
        if item[0] != "tool":
            return item

        state = item[1]

        return (
            "tool",
            state["id"],
            state["name"],
            state["title"],
            state["kind"],
            json.dumps(state["raw_input"], sort_keys=True),
            state["status"],
            state["text"],
            json.dumps(state["raw_output"], sort_keys=True),
        )


def result_text(update: schema.SessionToolCallUpdate) -> str:
    """Текст первого блока содержимого обновления вызова."""
    assert update.content is not None
    content = update.content[0]
    assert isinstance(content, schema.ContentToolCallContent)
    block = content.content
    assert isinstance(block, schema.TextContentBlock)

    return block.text


class AcpProcess:
    """Запущенный `boba acp`: соединение клиента ACP v2 поверх его stdio,
    сырые строки stdout и stderr в списках. Команда по умолчанию — модуль из
    venv; бинарник передаётся явно."""

    MODULE: ClassVar[str] = "boba.cli.app"
    LINE_LIMIT: ClassVar[int] = 16 * 1024 * 1024

    def __init__(
        self,
        root: Path,
        model: FakeModel,
        command: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if command is None:
            command = (sys.executable, "-m", self.MODULE)

        if environment is None:
            environment = {}

        self._command = list(command)
        self._root = root
        self._model = model
        self._environment = dict(environment)
        self._process: asyncio.subprocess.Process | None = None
        self.raw_stdout: list[str] = []
        self.stderr: list[str] = []
        self.frames = Frames()
        self._tasks: list[asyncio.Task[None]] = []
        self._reader = asyncio.StreamReader(limit=self.LINE_LIMIT)
        self._connection: ClientConnection | None = None

    @property
    def process(self) -> asyncio.subprocess.Process:
        assert self._process is not None
        return self._process

    def settings_path(self) -> Path:
        return self._root / "settings.json"

    def journal(self, session_id: str) -> list[Document]:
        path = self._root / "history" / f"{session_id}.jsonl"
        found: list[Document] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            found.append(json.loads(raw))

        return found

    def settings_document(
        self, mode: str = "default", allow_bypass: bool = False
    ) -> Document:
        return {
            "model": "fake",
            "models": {"fake": self.profile_document("fake-model")},
            "history": {"kind": "jsonl", "root": str(self._root / "history")},
            "permissions": {"defaultMode": mode, "allowBypass": allow_bypass},
        }

    def profile_document(
        self,
        model: str,
        sampling: Mapping[str, Any] | None = None,
        context_window: int = 32000,
        max_output_tokens: int = 4096,
        chars_per_token: int = 3,
    ) -> Document:
        """Профиль `models.<имя>` над фейковой моделью."""
        if sampling is None:
            sampling = {}

        return {
            "provider": {
                "kind": "openai",
                "baseUrl": self._model.base_url(),
                "apiKey": API_KEY,
            },
            "model": model,
            "sampling": dict(sampling),
            "contextWindow": context_window,
            "maxOutputTokens": max_output_tokens,
            "charsPerToken": chars_per_token,
        }

    def write_settings(self, document: Document) -> Path:
        path = self.settings_path()
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(OWNER_ONLY)

        return path

    async def spawn(self, *extra: str) -> None:
        """Запускает процесс по уже записанному файлу настроек."""
        environment = {
            "HOME": str(self._root / "home"),
            "PATH": os.environ["PATH"],
            **self._environment,
        }
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            "acp",
            "--settings",
            str(self.settings_path()),
            "--setting-sources",
            "user",
            *extra,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self.LINE_LIMIT,
            env=environment,
            cwd=str(self._root),
        )
        self._tasks.append(asyncio.create_task(self._tee_stdout()))
        self._tasks.append(asyncio.create_task(self._drain_stderr()))

    async def start(
        self,
        client: Client,
        registration: StandRegistration,
        document: Document | None = None,
        *extra: str,
    ) -> ClientConnection:
        """Файл настроек, процесс и соединение клиента."""
        if document is None:
            document = self.settings_document()

        self.write_settings(document)
        await self.spawn(*extra)

        return self.connect(client, registration)

    def connect(
        self, client: Client, registration: StandRegistration
    ) -> ClientConnection:
        stdin = self.process.stdin
        assert stdin is not None
        connection = ClientConnection(
            client, registration.answer, stdin, self._reader, observers=[self.frames]
        )
        registration.connection = connection
        self._connection = connection

        return connection

    async def turn(
        self,
        client: AcpClient,
        session_id: str,
        prompt: Sequence[schema.TextContentBlock | schema.ResourceContentBlock],
    ) -> tuple[schema.PromptResponse, schema.IdleSessionStateUpdate | IdleErrorUpdate]:
        """Ход целиком: `session/prompt` и ожидание его `idle`."""
        assert self._connection is not None
        response = await self._connection.prompt(
            session_id=session_id, prompt=list(prompt)
        )

        return response, await client.idle(session_id)

    async def close_stdin(self) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.close()
        await stdin.wait_closed()

    async def wait(self) -> int:
        """Ждёт конца процесса и сверяет все его кадры со схемой ACP."""
        code = await asyncio.wait_for(self.process.wait(), READ_TIMEOUT_SEC)
        for task in self._tasks:
            await task

        self.frames.check_schema()

        return code

    async def kill(self) -> None:
        """Добивает живой процесс и сверяет все его кадры со схемой ACP."""
        if self._process is None:
            return

        if self._process.returncode is None:
            self._process.kill()
            await asyncio.wait_for(self._process.wait(), READ_TIMEOUT_SEC)

        self.frames.check_schema()

    def assert_stdout_is_frames_only(self) -> None:
        assert self.raw_stdout, self.stderr[-10:]
        for line in self.raw_stdout:
            frame = json.loads(line)
            assert frame["jsonrpc"] == "2.0", line

    async def _tee_stdout(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        while True:
            raw = await stdout.readline()
            if not raw:
                self._reader.feed_eof()
                return

            self.raw_stdout.append(raw.decode("utf-8").rstrip("\n"))
            self._reader.feed_data(raw)

    async def _drain_stderr(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        while True:
            raw = await stderr.readline()
            if not raw:
                return

            self.stderr.append(raw.decode("utf-8", errors="replace").rstrip("\n"))


@pytest.fixture
async def boba(model: FakeModel, tmp_path: Path) -> AsyncIterator[AcpProcess]:
    process = AcpProcess(tmp_path, model)
    try:
        yield process
    finally:
        await process.kill()


async def handshake(
    conn: ClientConnection, forms: bool = False
) -> schema.InitializeResponse:
    """Рукопожатие; forms — клиент объявляет формы `elicitation.form`."""
    capabilities = schema.ClientCapabilities()
    if forms:
        elicitation = schema.ElicitationCapabilities(
            form=schema.ElicitationFormCapabilities()
        )
        capabilities = schema.ClientCapabilities(elicitation=elicitation)

    return await conn.initialize(
        protocol_version=AcpVersion.V2,
        info=schema.Implementation(name="boba-tests", version="0"),
        capabilities=capabilities,
    )


class Steps:
    """Шаги сценария, общие для команды и бинарника."""

    @staticmethod
    async def tool_turn_streams_updates(
        boba: AcpProcess, client: AcpClient, session_id: str
    ) -> None:
        response, idle = await boba.turn(
            client,
            session_id,
            call({"name": "mcp__stand__add", "arguments": {"a": 1, "b": 2}}),
        )
        assert idle.stop_reason == StopReason.END_TURN

        updates = client.updates_of(session_id)
        assert isinstance(updates[0], schema.RunningSessionStateUpdate)
        echo = updates[1]
        assert isinstance(echo, schema.UserMessageUpdate)
        assert echo.message_id == response.message_id
        assert updates[-1] is idle

        start = client.of(schema.SessionToolCallUpdate)[0]
        assert start.name == "mcp__stand__add"
        assert start.title is not None
        assert start.title.startswith("mcp__stand__add ")
        assert start.kind == ToolKindName.READ
        assert start.status == ToolStatus.PENDING
        assert isinstance(start.raw_input, dict)
        assert start.raw_input["a"] == 1
        assert start.raw_input["b"] == 2
        assert client.permissions == []

        calls = client.calls_of(start.tool_call_id)
        assert [c.status for c in calls] == [
            ToolStatus.PENDING,
            ToolStatus.IN_PROGRESS,
            ToolStatus.COMPLETED,
        ]
        done = calls[-1]
        assert result_text(done) == "3"
        assert done.raw_output == {
            "content": [{"type": "text", "text": "3"}],
            "isError": False,
            "resultType": "complete",
        }

        chunks = client.of(schema.AgentMessageChunk)
        assert chunks
        assert all(chunk.message_id for chunk in chunks)

    @staticmethod
    async def permission_is_asked_and_remembered(
        boba: AcpProcess, client: AcpClient, session_id: str
    ) -> None:
        client.choice = PermissionChoice.ALLOW_ALWAYS
        _, idle = await boba.turn(
            client,
            session_id,
            call({"name": "mcp__stand__shout", "arguments": {"text": "hi"}}),
        )
        assert idle.stop_reason == StopReason.END_TURN
        assert len(client.permissions) == 1
        asked = client.permissions[0]
        assert asked.name == "mcp__stand__shout"
        assert asked.title is not None
        assert asked.title.startswith("mcp__stand__shout ")
        assert asked.kind == ToolKindName.EDIT
        assert asked.status == ToolStatus.PENDING
        assert [o.kind for o in client.options[0]] == [
            "allow_once",
            "allow_always",
            "reject_once",
        ]
        done = client.calls_of(asked.tool_call_id)[-1]
        assert done.status == ToolStatus.COMPLETED
        assert result_text(done) == "HI"

        _, again = await boba.turn(
            client,
            session_id,
            call({"name": "mcp__stand__shout", "arguments": {"text": "again"}}),
        )
        assert again.stop_reason == StopReason.END_TURN
        assert len(client.permissions) == 1


class TestHandshake:
    async def test_initialize_reports_the_agent_and_its_capabilities(
        self, boba: AcpProcess, registration: StandRegistration
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            response = await handshake(conn)

            assert response.protocol_version == AcpVersion.V2
            assert response.info.name == "boba"
            assert response.info.version
            assert response.capabilities is not None
            session = response.capabilities.session
            assert session is not None
            assert session.mcp is not None
            assert session.mcp.acp is not None
            assert session.mcp.http is None
            assert session.mcp.stdio is None
            assert session.prompt is not None
            assert session.prompt.image is None
            assert session.fork is not None
            assert session.delete is not None

            with pytest.raises(RequestError) as refused:
                await conn.login(method_id="none")

            assert refused.value.code == AcpErrorCode.METHOD_NOT_FOUND
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        boba.assert_stdout_is_frames_only()


class TestTurnState:
    async def test_prompt_answers_the_message_id_and_idle_ends_the_turn(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = session.session_id
            response, idle = await boba.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )

            assert idle.stop_reason == StopReason.END_TURN
            updates = client.updates_of(session_id)
            assert isinstance(updates[0], schema.RunningSessionStateUpdate)
            echo = updates[1]
            assert isinstance(echo, schema.UserMessageUpdate)
            assert echo.message_id == response.message_id
            assert FeedView().user_texts(FeedView().render(updates)) == [
                ScenarioName.ANSWER.value
            ]
            assert updates[-1] is idle
            assert len(client.of(schema.RunningSessionStateUpdate)) == 1
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        human = [
            r["uuid"]
            for r in boba.journal(session_id)
            if r["type"] == "user" and r.get("origin", {}).get("kind") == "human"
        ]
        assert human == [response.message_id]

    async def test_model_failure_after_insertion_is_idle_with_error(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        document = boba.settings_document()
        document["limits"] = {"apiRetries": 0}
        conn = await boba.start(client, registration, document)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            broken = f"{ScenarioName.ANSWER.value} {FailureName.STATUS_500.value}"
            response, idle = await boba.turn(
                client, session.session_id, [schema.TextContentBlock(text=broken)]
            )

            assert response.message_id
            assert isinstance(idle, IdleErrorUpdate)
            assert idle.stop_reason == StopReason.ERROR
            assert idle.error is not None
            assert idle.error.code == AcpErrorCode.INTERNAL_ERROR
            assert isinstance(idle.error.data, dict)
            assert idle.error.data["kind"] == "api_error"
            assert idle.error.message
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


class TestConversation:
    async def test_tool_turn_permission_and_stdout_discipline(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration, None, "--record-requests")
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            session_id = session.session_id
            assert session_id

            await Steps.tool_turn_streams_updates(boba, client, session_id)
            await Steps.permission_is_asked_and_remembered(boba, client, session_id)
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        boba.assert_stdout_is_frames_only()
        assert any("INFO" in line for line in boba.stderr)

        records = boba.journal(session_id)
        types = [r["type"] for r in records]
        assert "api-request" in types
        assert "permission-rule" in types
        journal = json.dumps(records)
        assert API_KEY not in journal
        assert API_KEY not in "\n".join(boba.raw_stdout)
        assert API_KEY not in "\n".join(boba.stderr)

    async def test_secrets_stay_in_the_settings_file(
        self, boba: AcpProcess, registration: StandRegistration
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            pid = boba.process.pid
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            environ = Path(f"/proc/{pid}/environ").read_bytes()

            assert API_KEY.encode() not in cmdline
            assert API_KEY.encode() not in environ
            assert str(boba.settings_path()).encode() in cmdline
            assert stat.S_IMODE(boba.settings_path().stat().st_mode) == OWNER_ONLY
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_rejected_permission_fails_the_call(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        client.choice = PermissionChoice.REJECT_ONCE
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            _, idle = await boba.turn(
                client,
                session.session_id,
                call({"name": "mcp__stand__shout", "arguments": {"text": "no"}}),
            )

            assert idle.stop_reason == StopReason.END_TURN
            assert len(client.permissions) == 1
            calls = client.calls_of(client.permissions[0].tool_call_id)
            assert [c.status for c in calls] == [ToolStatus.PENDING, ToolStatus.FAILED]
            assert "reject" in result_text(calls[-1]).lower()
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_call_progress_becomes_tool_call_update(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            slow = {
                "name": "mcp__stand__progress",
                "arguments": {"steps": 3, "seconds": 0.1},
            }
            _, idle = await boba.turn(client, session.session_id, call(slow))

            assert idle.stop_reason == StopReason.END_TURN
            start = client.of(schema.SessionToolCallUpdate)[0]
            reports: list[str] = []
            bare: list[schema.SessionToolCallUpdate] = []
            for update in client.calls_of(start.tool_call_id):
                if update.status != ToolStatus.IN_PROGRESS:
                    continue

                if update.content is None:
                    if update.field_meta is not None:
                        bare.append(update)
                    continue

                reports.append(result_text(update))

            # уведомление без текста и итога — только _meta, без строки «0»
            assert [update.field_meta for update in bare] == [StandRegistration.JOURNAL]

            assert reports == [
                "step 1 of 3 — 1 of 3",
                "step 2 of 3 — 2 of 3",
                "step 3 of 3 — 3 of 3",
            ]
            done = client.calls_of(start.tool_call_id)[-1]
            assert done.status == ToolStatus.COMPLETED
            assert result_text(done) == "did 3 steps"
            call_request = registration.received[-1]
            assert call_request.params is not None
            meta = call_request.params["_meta"]
            assert meta["progressToken"] == start.tool_call_id
            assert meta[mt.PROTOCOL_VERSION_META_KEY] == McpVersion.V2026_07_28
            assert meta[mt.CLIENT_CAPABILITIES_META_KEY] == {}
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_resource_link_reaches_the_model_and_comes_back_in_the_replay(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            _, idle = await boba.turn(
                client,
                session.session_id,
                [
                    schema.TextContentBlock(
                        text=f"{ScenarioName.ANSWER.value} look at "
                    ),
                    schema.ResourceContentBlock.model_validate(
                        {"name": "main.py", "uri": "file:///work/main.py"}
                    ),
                ],
            )

            assert idle.stop_reason == StopReason.END_TURN
            sent = model.app.requests[0]["messages"][-1]["content"]
            assert "[@main.py](file:///work/main.py)" in sent

            # ветка копирует записи как есть: вложение остаётся блоком
            forked = await conn.fork_session(
                session_id=session.session_id, cwd=str(tmp_path), mcp_servers=[]
            )
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=forked.session_id,
                mcp_servers=[],
                replay_from=schema.ReplayFromStartVariant(),
            )
            asked = [
                update
                for update in client.updates_of(forked.session_id)
                if isinstance(update, schema.UserMessageUpdate)
            ]
            assert len(asked) == 1, asked
            content = asked[0].content
            assert content is not None
            text, link = content
            assert isinstance(text, schema.TextContentBlock)
            assert text.text == f"{ScenarioName.ANSWER.value} look at "
            assert isinstance(link, schema.ResourceContentBlock)
            assert (link.name, str(link.uri)) == ("main.py", "file:///work/main.py")
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_context_for_the_model_is_not_the_question_in_the_replay(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        """Текст только для модели (контекст приложения клиента) модель
        получает, а воспроизведение показывает вопрос без него."""
        client = AcpClient()
        conn = await boba.start(client, registration)
        context = schema.TextContentBlock(
            text="the board shows three cells",
            annotations=schema.Annotations(audience=["assistant"]),
        )
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            _, idle = await boba.turn(
                client,
                session.session_id,
                [
                    schema.TextContentBlock(text=f"{ScenarioName.ANSWER.value} go"),
                    context,
                ],
            )

            assert idle.stop_reason == StopReason.END_TURN
            sent = json.dumps(model.app.requests[0]["messages"])
            assert "the board shows three cells" in sent

            await conn.close_session(session_id=session.session_id)
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session.session_id,
                mcp_servers=[],
                replay_from=schema.ReplayFromStartVariant(),
            )
            asked = [
                update
                for update in client.updates_of(session.session_id)
                if isinstance(update, schema.UserMessageUpdate)
            ]
            replayed = asked[-1].content
            assert replayed is not None
            assert [block.model_dump(exclude_none=True) for block in replayed] == [
                {"type": "text", "text": f"{ScenarioName.ANSWER.value} go"}
            ]
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


class TestMcpOverAcp:
    """Инструменты только через MCP-over-ACP: регистрация отвечает сама."""

    async def test_list_pages_arrive_before_session_new_answers(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            await boba.turn(
                client,
                session.session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        methods: list[str] = []
        cursors: list[object] = []
        for message in registration.received:
            methods.append(message.method)
            assert message.server_id == StandRegistration.STAND.server_id
            assert message.params is not None
            meta = message.params["_meta"]
            assert meta[mt.PROTOCOL_VERSION_META_KEY] == McpVersion.V2026_07_28
            assert meta[mt.CLIENT_CAPABILITIES_META_KEY] == {}
            assert meta[mt.CLIENT_INFO_META_KEY]["name"] == "boba"
            if message.method == "tools/list":
                cursors.append(message.params.get("cursor"))

        assert methods == ["server/discover"] + ["tools/list"] * 4
        assert cursors == [None, "3", "6", "9"]

        answered_at = boba.frames.position_of_response(
            boba.frames.request_id("session/new")
        )
        asked_at: list[int] = []
        for position, event in enumerate(boba.frames.events):
            message = event.message
            incoming = event.direction is StreamDirection.INCOMING
            if incoming and message.get("method") == "mcp/message":
                asked_at.append(position)

        assert len(asked_at) == 5
        assert max(asked_at) < answered_at

        offered: list[str] = []
        for tool in model.app.requests[0]["tools"]:
            offered.append(tool["function"]["name"])

        expected: list[str] = []
        for tool in RegisteredTool:
            expected.append(f"mcp__stand__{tool.value}")

        assert sorted(offered) == sorted(expected)

    async def test_internal_and_external_errors_differ(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, registration, document)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            both = call(
                {"name": "mcp__stand__gone"},
                {"name": "mcp__stand__retired"},
                {"name": "mcp__stand__broken"},
            )
            await boba.turn(client, session.session_id, both)

            outcomes: list[schema.SessionToolCallUpdate] = []
            for start in client.of(schema.SessionToolCallUpdate):
                if start.status != ToolStatus.PENDING:
                    continue

                outcomes.append(client.calls_of(start.tool_call_id)[-1])

            gone, retired, broken = outcomes
            assert gone.status == ToolStatus.FAILED
            assert "got ACP error -33002" in result_text(gone)
            assert isinstance(gone.raw_output, dict)
            assert gone.raw_output["error_kind"] == "mcp_transport_error"

            assert retired.status == ToolStatus.FAILED
            assert "error -32602: Unknown tool: retired" in result_text(retired)
            assert '"tool": "retired"' in result_text(retired)
            assert isinstance(retired.raw_output, dict)
            assert retired.raw_output["error_kind"] == "mcp_tool_error"

            assert broken.status == ToolStatus.FAILED
            assert result_text(broken) == StandRegistration.FAILURE
            assert isinstance(broken.raw_output, dict)
            assert broken.raw_output["isError"] is True
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_input_required_without_questions_has_a_round_limit(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, registration, document)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            asking = call({"name": "mcp__stand__asking"})
            await boba.turn(client, session.session_id, asking)

            done = client.of(schema.SessionToolCallUpdate)[-1]
            assert done.status == ToolStatus.FAILED
            assert "asked for user input more than 3 times" in result_text(done)
            sent: list[McpMessage] = []
            for message in registration.received:
                if message.method == "tools/call":
                    sent.append(message)

            assert len(sent) == 4
            assert sent[0].params is not None
            assert "requestState" not in sent[0].params
            assert sent[-1].params is not None
            assert sent[-1].params["requestState"] == "opaque"
            assert sent[-1].params["inputResponses"] == {}
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_user_only_blocks_do_not_reach_the_model(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            mixed = call({"name": "mcp__stand__blocks"})
            await boba.turn(client, session.session_id, mixed)

            done = client.of(schema.SessionToolCallUpdate)[-1]
            assert done.status == ToolStatus.COMPLETED
            assert isinstance(done.raw_output, dict)
            assert len(done.raw_output["content"]) == 3
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        returned = model.app.requests[-1]["messages"][-1]
        assert returned["role"] == "tool"
        assert "for the model" in returned["content"]
        assert "[resource_link report.csv text/csv]" in returned["content"]
        assert "for the user" not in returned["content"]

    async def test_two_sessions_share_one_registration(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            first = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            second = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            await asyncio.gather(
                boba.turn(
                    client,
                    first.session_id,
                    call({"name": "mcp__stand__add", "arguments": {"a": 1, "b": 2}}),
                ),
                boba.turn(
                    client,
                    second.session_id,
                    call({"name": "mcp__stand__add", "arguments": {"a": 10, "b": 20}}),
                ),
            )

            answers: list[str] = []
            for session_id in (first.session_id, second.session_id):
                updated = [
                    u
                    for u in client.updates_of(session_id)
                    if isinstance(u, schema.SessionToolCallUpdate)
                ]
                answers.append(result_text(updated[-1]))

            assert answers == ["3", "30"]
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        calls: list[str] = []
        for message in registration.received:
            if message.method == "tools/call":
                calls.append(message.request_id)

        assert len(calls) == 2
        assert len(set(calls)) == 2


class TestQueueAndCancel:
    async def test_second_prompt_waits_for_its_own_turn(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, registration, document)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            session_id = session.session_id
            slow = {
                "name": "mcp__stand__progress",
                "arguments": {"steps": 1, "seconds": 1.0},
            }
            first, second = await asyncio.gather(
                conn.prompt(
                    session_id=session_id,
                    prompt=[*call(slow)],
                ),
                conn.prompt(
                    session_id=session_id,
                    prompt=[schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
                ),
            )
            ended = await client.idle(session_id)
            assert ended.stop_reason == StopReason.END_TURN
            again = await client.idle(session_id)
            assert again.stop_reason == StopReason.END_TURN

            assert first.message_id != second.message_id
            echoes = [
                u
                for u in client.updates_of(session_id)
                if isinstance(u, schema.UserMessageUpdate)
            ]
            assert [e.message_id for e in echoes] == [
                first.message_id,
                second.message_id,
            ]
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        records = boba.journal(session_id)
        spoken = [
            r["message"]["content"]
            for r in records
            if r["type"] == "user" and isinstance(r["message"]["content"], str)
        ]
        assert len(spoken) == 2
        assert ScenarioName.ANSWER.value in spoken[1]

    async def test_cancel_stops_the_running_tool(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, registration, document)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            slow = {
                "name": "mcp__stand__progress",
                "arguments": {"steps": 1, "seconds": 30},
            }
            await conn.prompt(
                session_id=session.session_id,
                prompt=[*call(slow)],
            )
            while not client.of(schema.SessionToolCallUpdate):
                await asyncio.sleep(0.05)

            while registration.received[-1].method != "tools/call":
                await asyncio.sleep(0.05)

            await conn.cancel_session(session_id=session.session_id)
            idle = await client.idle(session.session_id)

            assert idle.stop_reason == StopReason.CANCELLED
            start = client.of(schema.SessionToolCallUpdate)[0]
            assert client.calls_of(start.tool_call_id)[-1].status == ToolStatus.FAILED
            async with asyncio.timeout(READ_TIMEOUT_SEC):
                while not registration.cancelled:
                    await asyncio.sleep(0.05)

            assert registration.cancelled == [registration.received[-1].request_id]
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_cancel_withdraws_a_pending_permission_question(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        client.hold = asyncio.Event()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            await conn.prompt(
                session_id=session.session_id,
                prompt=[
                    *call({"name": "mcp__stand__shout", "arguments": {"text": "x"}})
                ],
            )
            asked = await client.next_permission(0)
            await conn.cancel_session(session_id=session.session_id)
            idle = await client.idle(session.session_id)

            assert idle.stop_reason == StopReason.CANCELLED
            assert client.calls_of(asked.tool_call_id)[-1].status == ToolStatus.FAILED

            client.hold.set()
            await asyncio.sleep(0.2)
            _, again = await boba.turn(
                client,
                session.session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert again.stop_reason == StopReason.END_TURN
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


class TestRefusals:
    async def test_foreign_blocks_sessions_and_servers_are_refused(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            with pytest.raises(RequestError) as relative:
                await conn.new_session(cwd="relative/dir", mcp_servers=[])

            assert relative.value.code == AcpErrorCode.INVALID_PARAMS

            for foreign in (
                schema.HttpMcpServer.model_validate(
                    {"name": "web", "url": "http://127.0.0.1:9/mcp"}
                ),
                schema.StdioMcpServer(name="local", command="server"),
            ):
                with pytest.raises(RequestError) as refused:
                    await conn.new_session(cwd=str(tmp_path), mcp_servers=[foreign])

                assert refused.value.code == AcpErrorCode.INVALID_PARAMS
                assert f"type '{foreign.type}' is refused" in str(refused.value.data)

            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            with pytest.raises(RequestError) as image:
                await conn.prompt(
                    session_id=session.session_id,
                    prompt=[
                        schema.ImageContentBlock(data="AAAA", mime_type="image/png")
                    ],
                )

            assert image.value.code == AcpErrorCode.INVALID_PARAMS

            with pytest.raises(RequestError) as unknown:
                await conn.prompt(
                    session_id="no-such-session",
                    prompt=[schema.TextContentBlock(text="hello")],
                )

            assert unknown.value.code == AcpErrorCode.RESOURCE_NOT_FOUND
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_unreachable_mcp_server_is_left_out_with_a_notice(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        """Сервер, который не открылся, сессия не берёт: клиент получает
        пометку с причиной, а ход идёт с инструментами остальных серверов."""
        alive = schema.AcpMcpServer(name="alive", server_id="alive:1")
        dead = schema.AcpMcpServer(name="dead", server_id="dead:0")
        client = AcpClient()
        conn = await boba.start(client, StandRegistration([alive]))
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[alive, dead]
            )
            added = call({"name": "mcp__alive__add", "arguments": {"a": 1, "b": 2}})
            await boba.turn(client, session.session_id, added)
            notices = client.of(schema.SessionNotice)
            starts = client.of(schema.SessionToolCallUpdate)
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        assert len(notices) == 1, notices
        assert notices[0].severity == "warning", notices
        assert "'dead'" in notices[0].title, notices
        assert "dead:0" in str(notices[0].description), notices
        assert result_text(client.calls_of(starts[0].tool_call_id)[-1]) == "3"

    async def test_same_tool_of_two_servers_is_called_on_each(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        """Одноимённые инструменты двух серверов не сталкиваются: модель
        видит оба под полными именами и вызов уходит названному серверу."""
        first = schema.AcpMcpServer(name="first", server_id="first:1")
        second = schema.AcpMcpServer(name="second", server_id="second:2")
        registration = StandRegistration([first, second])
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[first, second]
            )
            both = call(
                {"name": "mcp__first__add", "arguments": {"a": 1, "b": 2}},
                {"name": "mcp__second__add", "arguments": {"a": 10, "b": 20}},
            )
            await boba.turn(client, session.session_id, both)

            starts: list[schema.SessionToolCallUpdate] = []
            for update in client.of(schema.SessionToolCallUpdate):
                if update.status == ToolStatus.PENDING:
                    starts.append(update)

            assert [start.name for start in starts] == [
                "mcp__first__add",
                "mcp__second__add",
            ]
            answers: list[str] = []
            for start in starts:
                answers.append(result_text(client.calls_of(start.tool_call_id)[-1]))

            assert answers == ["3", "30"]
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        called: list[str] = []
        for message in registration.received:
            if message.method == "tools/call":
                called.append(message.server_id)

        assert called == [first.server_id, second.server_id]

    async def test_server_name_that_breaks_tool_names_is_refused(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            broken = schema.AcpMcpServer(name="my__srv", server_id="my:1")
            with pytest.raises(RequestError) as refused:
                await conn.new_session(cwd=str(tmp_path), mcp_servers=[broken])

            assert refused.value.code == AcpErrorCode.INVALID_PARAMS
            assert "'my__srv'" in str(refused.value.data)
            assert "contains '__'" in str(refused.value.data)
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


class TestQuestions:
    """Вопрос AskUserQuestion — форма `elicitation/create` клиенту с
    toolCallId вызова; ответ формы — ответы вопроса для модели."""

    ASKED: ClassVar[dict[str, Any]] = {
        "questions": [
            {
                "question": "Which layout should the report page use?",
                "header": "Layout",
                "options": [
                    {
                        "label": "Single column",
                        "description": "Filters above the table.",
                        "preview": "| Filters |\n| Table |",
                    },
                    {"label": "Two columns", "description": "Filters on the side."},
                ],
            },
            {
                "question": "Which checks should run?",
                "header": "Checks",
                "options": [
                    {"label": "Unit tests", "description": "Fast."},
                    {"label": "UI tests", "description": "Slow."},
                ],
                "multiSelect": True,
            },
        ]
    }

    async def _asked(
        self,
        boba: AcpProcess,
        client: AcpClient,
        registration: StandRegistration,
        tmp_path: Path,
        forms: bool,
    ) -> tuple[str, schema.IdleSessionStateUpdate | IdleErrorUpdate]:
        """Ход с вызовом AskUserQuestion: id сессии и idle хода."""
        conn = await boba.start(client, registration)
        try:
            await handshake(conn, forms=forms)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            _, idle = await boba.turn(
                client,
                session.session_id,
                call({"name": "AskUserQuestion", "arguments": self.ASKED}),
            )
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

        return session.session_id, idle

    async def test_question_is_a_form_and_its_answer_reaches_the_model(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        """Поле на вопрос с вариантами и их описаниями, предпросмотр и пара
        «Other» в `_meta`; сессия в requires_action, пока вопрос открыт;
        ответ формы со своим текстом даёт модели второй текст ответов."""
        client = AcpClient()
        client.replies.append(
            schema.AcceptElicitationResponse(
                content={
                    "q1": "Single column",
                    "q2": ["UI tests", "Unit tests"],
                    "q2.other": "smoke test",
                }
            )
        )

        session_id, idle = await self._asked(
            boba, client, registration, tmp_path, forms=True
        )

        assert idle.stop_reason == StopReason.END_TURN
        assert client.permissions == []
        names: list[str] = []
        for tool in model.app.requests[0]["tools"]:
            names.append(tool["function"]["name"])

        assert "AskUserQuestion" in names
        (form,) = client.forms
        call_update = client.of(schema.SessionToolCallUpdate)[0]
        assert call_update.name == "AskUserQuestion"
        assert form.session_id == session_id
        assert form.tool_call_id == call_update.tool_call_id
        assert form.meta == {}
        assert form.message == (
            "Which layout should the report page use?\nWhich checks should run?"
        )
        requested = form.requested.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        assert requested == {
            "type": "object",
            "properties": {
                "q1": {
                    "type": "string",
                    "title": "Layout",
                    "description": "Which layout should the report page use?",
                    "oneOf": [
                        {
                            "const": "Single column",
                            "title": "Single column",
                            "description": "Filters above the table.",
                            "_meta": {"boba/preview": "| Filters |\n| Table |"},
                        },
                        {
                            "const": "Two columns",
                            "title": "Two columns",
                            "description": "Filters on the side.",
                        },
                    ],
                    "_meta": {"boba/other": "q1.other"},
                },
                "q1.other": {"type": "string", "title": "Other"},
                "q2": {
                    "type": "array",
                    "title": "Checks",
                    "description": "Which checks should run?",
                    "items": {
                        "anyOf": [
                            {
                                "const": "Unit tests",
                                "title": "Unit tests",
                                "description": "Fast.",
                            },
                            {
                                "const": "UI tests",
                                "title": "UI tests",
                                "description": "Slow.",
                            },
                        ]
                    },
                    "_meta": {"boba/other": "q2.other"},
                },
                "q2.other": {"type": "string", "title": "Other"},
            },
        }

        states: list[str] = []
        for update in client.updates_of(session_id):
            if isinstance(
                update,
                schema.RequiresActionSessionStateUpdate
                | schema.RunningSessionStateUpdate,
            ):
                states.append(update.state)

        assert states == ["running", "requires_action", "running"]

        done = client.calls_of(call_update.tool_call_id)[-1]
        assert done.status == ToolStatus.COMPLETED
        assert done.raw_output == {
            "questions": self.ASKED["questions"],
            "answers": {
                "Which layout should the report page use?": "Single column",
                "Which checks should run?": "UI tests, Unit tests, smoke test",
            },
            "picks": {
                "Which layout should the report page use?": {
                    "labels": ["Single column"]
                },
                "Which checks should run?": {
                    "labels": ["UI tests", "Unit tests"],
                    "other": "smoke test",
                },
            },
            "outcome": "accept",
        }
        returned = model.app.requests[-1]["messages"][-1]
        assert returned["role"] == "tool"
        assert returned["content"].startswith(
            'The user answered: "Which layout should the report page use?"='
            '"Single column", "Which checks should run?"="UI tests, Unit tests, '
            'smoke test".'
        )

    async def test_declined_question_is_a_rejection(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        client = AcpClient()
        client.replies.append(schema.DeclineElicitationResponse())

        _, idle = await self._asked(boba, client, registration, tmp_path, forms=True)

        assert idle.stop_reason == StopReason.END_TURN
        call_update = client.of(schema.SessionToolCallUpdate)[0]
        done = client.calls_of(call_update.tool_call_id)[-1]
        assert done.status == ToolStatus.FAILED
        assert done.raw_output == {
            "questions": self.ASKED["questions"],
            "outcome": "decline",
        }
        returned = model.app.requests[-1]["messages"][-1]
        assert (
            "The user doesn't want to proceed with this tool use."
            in (returned["content"])
        )

    async def test_client_without_forms_gets_no_question_tool(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        """Клиент без форм: модель не видит инструмента вопроса, и вызов его —
        «нет такого инструмента», ни формы, ни запроса разрешения."""
        client = AcpClient()

        _, idle = await self._asked(boba, client, registration, tmp_path, forms=False)

        assert idle.stop_reason == StopReason.END_TURN
        assert client.forms == []
        assert client.permissions == []
        names: list[str] = []
        for tool in model.app.requests[0].get("tools", []):
            names.append(tool["function"]["name"])

        assert "AskUserQuestion" not in names
        returned = model.app.requests[-1]["messages"][-1]
        assert "No such tool available: AskUserQuestion" in returned["content"]

    async def _server_asked(
        self,
        boba: AcpProcess,
        client: AcpClient,
        registration: StandRegistration,
        tmp_path: Path,
        forms: bool,
    ) -> tuple[schema.SessionToolCallUpdate, list[McpMessage]]:
        """Ход с вызовом инструмента стенда, который просит ввод: итог вызова
        и запросы tools/call, дошедшие до сервера."""
        conn = await boba.start(client, registration)
        try:
            await handshake(conn, forms=forms)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            _, idle = await boba.turn(
                client, session.session_id, call({"name": "mcp__stand__choosing"})
            )
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        assert idle.stop_reason == StopReason.END_TURN
        sent: list[McpMessage] = []
        for message in registration.received:
            if message.method == "tools/call":
                sent.append(message)

        return client.of(schema.SessionToolCallUpdate)[-1], sent

    async def test_server_question_is_a_form_and_the_call_is_repeated(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        """input_required сервера: форма клиенту с toolCallId вызова, именем
        сервера и инструмента в `_meta` (enumNames — вариантами oneOf), повтор
        вызова с ответом и тем же requestState; раунд — в `_meta` результата,
        модель получает ответ сервера после повтора."""
        client = AcpClient()
        client.replies.append(
            schema.AcceptElicitationResponse(content={"table": "sales.orders_stage"})
        )

        done, sent = await self._server_asked(
            boba, client, registration, tmp_path, forms=True
        )

        (form,) = client.forms
        assert form.tool_call_id == done.tool_call_id
        assert form.message == "choosing needs the target table before it can start"
        assert form.meta == {"boba/server": "stand", "boba/tool": "choosing"}
        requested = form.requested.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        assert requested == {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "title": "Target",
                    "description": "Which table should receive the rows?",
                    "oneOf": [
                        {"const": "sales.orders", "title": "Orders"},
                        {"const": "sales.orders_stage", "title": "Orders stage"},
                    ],
                }
            },
            "required": ["table"],
        }

        first, second = sent
        assert first.params is not None
        assert first.params["_meta"][mt.CLIENT_CAPABILITIES_META_KEY] == {
            "elicitation": {"form": {}}
        }
        assert second.params is not None
        assert second.params["requestState"] == StandRegistration.CHOOSE_STATE
        assert second.params["inputResponses"] == {
            "target": {"action": "accept", "content": {"table": "sales.orders_stage"}}
        }

        assert done.status == ToolStatus.COMPLETED
        assert result_text(done) == "rows go to sales.orders_stage"
        assert isinstance(done.raw_output, dict)
        rounds = done.raw_output["_meta"]["boba/elicitations"]
        assert rounds == [
            {
                "server": "stand",
                "tool": "choosing",
                "message": "choosing needs the target table before it can start",
                "requested": requested,
                "action": "accept",
                "content": {"table": "sales.orders_stage"},
            }
        ]
        returned = model.app.requests[-1]["messages"][-1]
        assert "rows go to sales.orders_stage" in returned["content"]

    async def test_declined_server_question_goes_back_to_the_server(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        client.replies.append(schema.DeclineElicitationResponse())

        done, sent = await self._server_asked(
            boba, client, registration, tmp_path, forms=True
        )

        assert len(sent) == 2
        assert sent[1].params is not None
        assert sent[1].params["inputResponses"] == {"target": {"action": "decline"}}
        assert done.status == ToolStatus.FAILED
        assert result_text(done) == "the user answered decline"
        assert isinstance(done.raw_output, dict)
        (asked,) = done.raw_output["_meta"]["boba/elicitations"]
        assert asked["action"] == "decline"
        assert "content" not in asked or asked["content"] == {}

    async def test_client_without_forms_is_not_asked_by_a_server(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        """Клиент без форм: агент не объявляет серверу elicitation, а
        просьбу сервера о вводе снимает сам (cancel), не спрашивая клиента."""
        client = AcpClient()

        done, sent = await self._server_asked(
            boba, client, registration, tmp_path, forms=False
        )

        assert client.forms == []
        assert sent[0].params is not None
        assert sent[0].params["_meta"][mt.CLIENT_CAPABILITIES_META_KEY] == {}
        assert sent[1].params is not None
        assert sent[1].params["inputResponses"] == {"target": {"action": "cancel"}}
        assert result_text(done) == "the user answered cancel"

    async def test_cancel_withdraws_the_form(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        """`session/cancel`, пока форма открыта: запрос снят у клиента, ход
        кончается cancelled."""
        client = AcpClient()
        client.hold_form = asyncio.Event()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn, forms=True)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            await conn.prompt(
                session_id=session.session_id,
                prompt=[*call({"name": "AskUserQuestion", "arguments": self.ASKED})],
            )
            await client.next_form(0)
            await conn.cancel_session(session_id=session.session_id)
            idle = await client.idle(session.session_id)
            for _ in range(50):
                if client.withdrawn:
                    break

                await asyncio.sleep(0.05)
        finally:
            client.hold_form.set()
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        assert idle.stop_reason == StopReason.CANCELLED
        assert client.withdrawn == 1


class TestLifecycle:
    async def test_sigterm_exits_zero_with_a_flushed_journal(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        await handshake(conn)
        session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        _, idle = await boba.turn(
            client,
            session.session_id,
            [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
        )
        assert idle.stop_reason == StopReason.END_TURN

        boba.process.send_signal(signal.SIGTERM)
        assert await boba.wait() == 0
        await conn.close()

        records = boba.journal(session.session_id)
        assert [r["type"] for r in records][-1] == "assistant"

    async def test_end_of_input_closes_the_sessions(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        await handshake(conn)
        session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        await conn.close()
        await boba.close_stdin()

        assert await boba.wait() == 0
        assert (tmp_path / "history" / f"{session.session_id}.jsonl").exists()
        boba.assert_stdout_is_frames_only()

    async def test_bad_settings_fail_before_the_protocol(
        self, boba: AcpProcess
    ) -> None:
        document = boba.settings_document()
        boba.write_settings(document)
        boba.settings_path().chmod(0o644)
        await boba.spawn()

        assert await boba.wait() == 1
        assert boba.raw_stdout == []
        assert any("chmod 600" in line for line in boba.stderr)


class TestHistory:
    """Сессии и история: второй процесс над тем же журналом."""

    PROMPTS: ClassVar[tuple[str, ...]] = (
        call({"name": "mcp__stand__add", "arguments": {"a": 2, "b": 3}})[0].text,
        ScenarioName.THINKING_ANSWER.value,
    )

    async def conversation(
        self, boba: AcpProcess, registration: StandRegistration, cwd: Path
    ) -> tuple[str, list[Any]]:
        """Первый процесс: сессия с двумя ходами; возвращает её id и живые
        уведомления; процесс завершён."""
        client = AcpClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, registration, document)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(cwd), mcp_servers=[StandRegistration.STAND]
            )
            for prompt in self.PROMPTS:
                _, idle = await boba.turn(
                    client, session.session_id, [schema.TextContentBlock(text=prompt)]
                )
                assert idle.stop_reason == StopReason.END_TURN
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

        return session.session_id, client.updates_of(session.session_id)

    async def test_replay_shows_the_conversation_like_the_live_turn(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        session_id, live = await self.conversation(boba, registration, tmp_path)

        second = AcpProcess(tmp_path, model)
        client = AcpClient()
        document = second.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await second.start(client, registration, document)
        try:
            await handshake(conn)
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[StandRegistration.STAND],
                replay_from=schema.ReplayFromStartVariant(),
            )
            replayed = client.updates_of(session_id)

            view = FeedView()
            replay_view = view.render(replayed)
            assert view.user_texts(replay_view) == list(self.PROMPTS)
            assert replay_view == view.render(live)
            assert len(view.without_user(replay_view)) >= 4

            request_id = second.frames.request_id("session/resume")
            answered_at = second.frames.position_of_response(request_id)
            before = second.frames.incoming_notifications(answered_at)
            assert len(before) == len(replayed)
            assert all(
                m["method"] != "session/update"
                for m in second.frames.incoming_after(answered_at)
            )

            _, idle = await second.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert idle.stop_reason == StopReason.END_TURN
        finally:
            await conn.close()
            await second.close_stdin()
            await second.kill()

        human = [
            r["message"]["content"]
            for r in second.journal(session_id)
            if r["type"] == "user" and r.get("origin", {}).get("kind") == "human"
        ]
        assert human == [*self.PROMPTS, ScenarioName.ANSWER.value]

    async def test_resume_restores_without_replay(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        session_id, _ = await self.conversation(boba, registration, tmp_path)

        second = AcpProcess(tmp_path, model)
        client = AcpClient()
        conn = await second.start(client, registration)
        try:
            await handshake(conn)
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[StandRegistration.STAND],
            )
            assert client.updates == []

            _, idle = await second.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert idle.stop_reason == StopReason.END_TURN
            assert client.of(schema.AgentMessageChunk)
        finally:
            await conn.close()
            await second.close_stdin()
            await second.kill()

    async def test_unknown_session_is_resource_not_found(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            for request in (
                conn.resume_session(
                    cwd=str(tmp_path),
                    session_id="no-such",
                    mcp_servers=[],
                    replay_from=schema.ReplayFromStartVariant(),
                ),
                conn.resume_session(
                    cwd=str(tmp_path), session_id="no-such", mcp_servers=[]
                ),
                conn.close_session(session_id="no-such"),
                conn.resume_session(
                    cwd=str(tmp_path), session_id="bad/id", mcp_servers=[]
                ),
            ):
                with pytest.raises(RequestError) as unknown:
                    await request

                assert unknown.value.code == AcpErrorCode.RESOURCE_NOT_FOUND

            with pytest.raises(RequestError) as relative:
                await conn.resume_session(
                    cwd="relative", session_id="x", mcp_servers=[]
                )

            assert relative.value.code == AcpErrorCode.INVALID_PARAMS

            with pytest.raises(RequestError) as foreign:
                await conn.resume_session(
                    cwd=str(tmp_path),
                    session_id="x",
                    mcp_servers=[],
                    replay_from=schema.OtherReplayFrom(type="message"),
                )

            assert foreign.value.code == AcpErrorCode.INVALID_PARAMS
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_close_frees_the_session_and_resume_reopens_it(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = session.session_id
            _, idle = await boba.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert idle.stop_reason == StopReason.END_TURN

            await conn.close_session(session_id=session_id)
            with pytest.raises(RequestError) as gone:
                await conn.prompt(
                    session_id=session_id, prompt=[schema.TextContentBlock(text="hi")]
                )

            assert gone.value.code == AcpErrorCode.RESOURCE_NOT_FOUND
            with pytest.raises(RequestError) as twice:
                await conn.close_session(session_id=session_id)

            assert twice.value.code == AcpErrorCode.RESOURCE_NOT_FOUND

            seen = len(client.updates_of(session_id))
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[],
                replay_from=schema.ReplayFromStartVariant(),
            )
            view = FeedView().render(client.updates_of(session_id)[seen:])
            assert [item[0] for item in view] == ["user", "agent"]
            assert view[0][2] == ScenarioName.ANSWER.value

            _, again = await boba.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert again.stop_reason == StopReason.END_TURN
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


class TestListAndDelete:
    PAGE: ClassVar[int] = 50

    @staticmethod
    async def all_pages(conn: ClientConnection, cwd: str) -> list[schema.SessionInfo]:
        """Все страницы списка с фильтром по каталогу: фильтр отбирает из
        страницы, поэтому страница может быть короче предела."""
        found: list[schema.SessionInfo] = []
        cursor: str | None = None
        while True:
            page = await conn.list_sessions(cwd=cwd, cursor=cursor)
            found.extend(page.sessions)
            cursor = page.next_cursor
            if cursor is None:
                return found

    async def test_list_pages_with_a_cursor_and_filters_by_cwd(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        quiet_cwd = tmp_path / "quiet"
        spoken_cwd = tmp_path / "spoken"
        try:
            await handshake(conn)
            created: list[str] = []
            for _ in range(self.PAGE + 2):
                session = await conn.new_session(cwd=str(quiet_cwd), mcp_servers=[])
                created.append(session.session_id)

            spoken = await conn.new_session(cwd=str(spoken_cwd), mcp_servers=[])
            question = f"{ScenarioName.ANSWER.value} what is the plan\nsecond line"
            await boba.turn(
                client, spoken.session_id, [schema.TextContentBlock(text=question)]
            )
            for session_id in [*created, spoken.session_id]:
                await conn.close_session(session_id=session_id)

            first = await conn.list_sessions()
            assert len(first.sessions) == self.PAGE
            assert first.next_cursor == first.sessions[-1].session_id
            second = await conn.list_sessions(cursor=first.next_cursor)
            assert len(second.sessions) == 3
            assert second.next_cursor is None

            listed = [s.session_id for s in [*first.sessions, *second.sessions]]
            assert listed == sorted(listed)
            assert set(listed) == {*created, spoken.session_id}
            assert all(s.updated_at for s in first.sessions)

            by_spoken = await conn.list_sessions(cwd=str(spoken_cwd))
            assert [s.session_id for s in by_spoken.sessions] == [spoken.session_id]
            assert (
                by_spoken.sessions[0].title
                == f"{ScenarioName.ANSWER.value} what is the plan"
            )
            assert by_spoken.sessions[0].cwd == str(spoken_cwd)

            by_process = await self.all_pages(conn, str(tmp_path))
            assert {s.session_id for s in by_process} == set(created)
            assert all(s.title is None for s in by_process)

            empty = await conn.list_sessions(cwd=str(tmp_path / "nowhere"))
            assert empty.sessions == []

            with pytest.raises(RequestError) as relative:
                await conn.list_sessions(cwd="relative")

            assert relative.value.code == AcpErrorCode.INVALID_PARAMS
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_delete_removes_the_session_and_repeats_silently(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = session.session_id
            await boba.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            journal = tmp_path / "history" / f"{session_id}.jsonl"
            assert journal.exists()

            await conn.delete_session(session_id=session_id)
            assert not journal.exists()
            listed = await conn.list_sessions()
            assert listed.sessions == []
            with pytest.raises(RequestError) as gone:
                await conn.prompt(
                    session_id=session_id, prompt=[schema.TextContentBlock(text="x")]
                )

            assert gone.value.code == AcpErrorCode.RESOURCE_NOT_FOUND

            await conn.delete_session(session_id=session_id)
            await conn.delete_session(session_id="never")
            await conn.delete_session(session_id="bad/id")
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


def update_kinds(frames: Frames, request_method: str) -> list[str]:
    """Виды `session/update`, пришедших до ответа на последний запрос метода."""
    request_id = frames.request_id(request_method, -1)
    answered_at = frames.position_of_response(request_id)
    kinds: list[str] = []
    for message in frames.incoming_notifications(answered_at):
        if message["method"] != "session/update":
            continue

        kinds.append(message["params"]["update"]["sessionUpdate"])

    assert all(
        m["method"] != "session/update" for m in frames.incoming_after(answered_at)
    )

    return kinds


def option_of(
    response: Any, option_id: ConfigOptionId
) -> schema.SelectSessionConfigOption:
    """Настройка `select` из ответа или уведомления с полным состоянием."""
    assert response.config_options is not None
    for option in response.config_options:
        if option.config_id == option_id.value:
            assert isinstance(option, schema.SelectSessionConfigOption)
            return option

    msg = f"configOption {option_id.value!r} is missing"
    raise AssertionError(msg)


def option_values(option: schema.SelectSessionConfigOption) -> list[str]:
    values: list[str] = []
    for item in option.options:
        assert isinstance(item, schema.SessionConfigSelectOption)
        values.append(item.value)

    return values


class TestSessionSettings:
    """Настройки сессии: ответы `session/new|resume`, `set_config_option` с
    уведомлением, режим `plan` в деле, смена модели между ходами и отказ при
    идущем ходе."""

    MODES: ClassVar[list[str]] = ["default", "acceptEdits", "plan", "dontAsk"]

    async def test_new_and_resume_report_config_options(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = session.session_id
            self.check_state(session, "default", "fake")
            mode = option_of(session, ConfigOptionId.MODE)
            for item in mode.options:
                assert isinstance(item, schema.SessionConfigSelectOption)
                assert item.name
                assert item.description

            await boba.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            await conn.close_session(session_id=session_id)
            replayed = await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[],
                replay_from=schema.ReplayFromStartVariant(),
            )
            self.check_state(replayed, "default", "fake")

            await conn.close_session(session_id=session_id)
            client.updates.clear()
            resumed = await conn.resume_session(
                cwd=str(tmp_path), session_id=session_id, mcp_servers=[]
            )
            self.check_state(resumed, "default", "fake")
            assert client.updates == []
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_bypass_is_offered_only_with_the_right(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, registration, document)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            mode = option_of(session, ConfigOptionId.MODE)
            assert mode.current_value == "bypassPermissions"
            assert option_values(mode) == [*self.MODES, "bypassPermissions"]
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_mode_option_changes_permissions_and_notifies(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            session_id = session.session_id
            await conn.set_config_option(
                session_id=session_id, config_id=ConfigOptionId.MODE.value, value="plan"
            )
            announced = client.of(schema.ConfigOptionUpdate)
            assert len(announced) == 1
            assert option_of(announced[0], ConfigOptionId.MODE).current_value == "plan"

            await boba.turn(
                client,
                session_id,
                call({"name": "mcp__stand__shout", "arguments": {"text": "x"}}),
            )
            assert client.permissions == []
            calls = client.calls_of(
                client.of(schema.SessionToolCallUpdate)[0].tool_call_id
            )
            assert [c.status for c in calls] == [ToolStatus.PENDING, ToolStatus.FAILED]
            assert "plan" in result_text(calls[-1]).lower()

            for value in ("bypassPermissions", "turbo"):
                with pytest.raises(RequestError) as refused:
                    await conn.set_config_option(
                        session_id=session_id,
                        config_id=ConfigOptionId.MODE.value,
                        value=value,
                    )

                assert refused.value.code == AcpErrorCode.INVALID_PARAMS

            changed = await conn.set_config_option(
                session_id=session_id,
                config_id=ConfigOptionId.MODE.value,
                value="default",
            )
            assert option_of(changed, ConfigOptionId.MODE).current_value == "default"
            assert option_of(changed, ConfigOptionId.MODEL).current_value == "fake"
            assert len(client.of(schema.ConfigOptionUpdate)) == 2

            await boba.turn(
                client,
                session_id,
                call({"name": "mcp__stand__shout", "arguments": {"text": "y"}}),
            )
            assert len(client.permissions) == 1
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        modes = [
            r["permissionMode"]
            for r in boba.journal(session_id)
            if r["type"] == "permission-mode"
        ]
        assert modes == ["default", "plan", "default"]

    async def test_model_switch_changes_the_profile_of_the_next_request(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        client = AcpClient()
        document = boba.settings_document()
        document["models"]["fake"]["sampling"] = {"temperature": 0.1}
        document["models"]["second"] = boba.profile_document(
            "second-model", sampling={"temperature": 0.9}
        )
        conn = await boba.start(client, registration, document, "--record-requests")
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = session.session_id
            assert option_values(option_of(session, ConfigOptionId.MODEL)) == [
                "fake",
                "second",
            ]
            await boba.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )

            changed = await conn.set_config_option(
                session_id=session_id,
                config_id=ConfigOptionId.MODEL.value,
                value="second",
            )
            assert option_of(changed, ConfigOptionId.MODEL).current_value == "second"
            announced = client.of(schema.ConfigOptionUpdate)
            assert len(announced) == 1
            assert option_of(announced[0], ConfigOptionId.MODEL).current_value == (
                "second"
            )

            _, idle = await boba.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert idle.stop_reason == StopReason.END_TURN
            assert [r["model"] for r in model.app.requests] == [
                "fake-model",
                "second-model",
            ]

            for config_id, value in (
                (ConfigOptionId.MODEL.value, "third"),
                ("temperature", "0.5"),
                (ConfigOptionId.MODEL.value, True),
            ):
                with pytest.raises(RequestError) as refused:
                    await conn.set_config_option(
                        session_id=session_id, config_id=config_id, value=value
                    )

                assert refused.value.code == AcpErrorCode.INVALID_PARAMS
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        records = boba.journal(session_id)
        temperatures = [
            r["params"]["sampling"]["temperature"]
            for r in records
            if r["type"] == "api-request"
        ]
        assert temperatures == [0.1, 0.9]
        announced_models = [
            r["attachment"]["identity"]["modelId"]
            for r in records
            if r["type"] == "attachment" and r["attachment"]["type"] == "model"
        ]
        assert announced_models == ["fake-model", "second-model"]

    async def test_model_switch_waits_for_an_idle_session(
        self, boba: AcpProcess, registration: StandRegistration, tmp_path: Path
    ) -> None:
        client = AcpClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        document["models"]["second"] = boba.profile_document("second-model")
        conn = await boba.start(client, registration, document)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
            )
            session_id = session.session_id
            slow = {
                "name": "mcp__stand__progress",
                "arguments": {"steps": 1, "seconds": 2.0},
            }
            await conn.prompt(session_id=session_id, prompt=[*call(slow)])
            while not client.of(schema.SessionToolCallUpdate):
                await asyncio.sleep(0.05)

            with pytest.raises(RequestError) as busy:
                await conn.set_config_option(
                    session_id=session_id,
                    config_id=ConfigOptionId.MODEL.value,
                    value="second",
                )

            assert busy.value.code == AcpErrorCode.INTERNAL_ERROR
            assert isinstance(busy.value.data, dict)
            assert "busy" in busy.value.data["details"]

            await conn.set_config_option(
                session_id=session_id,
                config_id=ConfigOptionId.MODE.value,
                value="default",
            )
            announced = client.of(schema.ConfigOptionUpdate)
            assert option_of(announced[-1], ConfigOptionId.MODE).current_value == (
                "default"
            )

            idle = await client.idle(session_id)
            assert idle.stop_reason == StopReason.END_TURN
            changed = await conn.set_config_option(
                session_id=session_id,
                config_id=ConfigOptionId.MODEL.value,
                value="second",
            )
            assert option_of(changed, ConfigOptionId.MODEL).current_value == "second"
            assert option_of(changed, ConfigOptionId.MODE).current_value == "default"
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    def check_state(self, response: Any, mode: str, model: str) -> None:
        assert response.config_options is not None
        assert [o.config_id for o in response.config_options] == ["mode", "model"]
        mode_option = option_of(response, ConfigOptionId.MODE)
        assert mode_option.category == "mode"
        assert mode_option.current_value == mode
        assert option_values(mode_option) == self.MODES
        model_option = option_of(response, ConfigOptionId.MODEL)
        assert model_option.category == "model"
        assert model_option.current_value == model
        assert option_values(model_option) == [model]


class TestServiceUpdates:
    """`usage_update` после каждого хода и после сжатия, `compaction_update`
    при сжатии, `session_info_update` с заголовком после первого хода и при
    воспроизведении; всё — до `idle` хода."""

    SIZE: ClassVar[int] = 32000 - 4096
    TITLE: ClassVar[str] = f"{ScenarioName.ANSWER.value} what is the plan"

    async def test_usage_and_title_follow_the_turn_and_the_replay(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        client = AcpClient()
        conn = await boba.start(client, registration)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = session.session_id
            await boba.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=f"{self.TITLE}\nsecond line")],
            )
            usage = client.of(schema.UsageUpdate)
            assert len(usage) == 1
            assert usage[0].size == self.SIZE
            assert 0 < usage[0].used < self.SIZE
            assert [t.title for t in client.of(schema.SessionInfoUpdate)] == [
                self.TITLE
            ]
            ending = client.updates_of(session_id)[-3:]
            assert [type(u) for u in ending] == [
                schema.UsageUpdate,
                schema.SessionInfoUpdate,
                schema.IdleSessionStateUpdate,
            ]

            await boba.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert len(client.of(schema.UsageUpdate)) == 2
            assert len(client.of(schema.SessionInfoUpdate)) == 1
            ending = client.updates_of(session_id)[-2:]
            assert [type(u) for u in ending] == [
                schema.UsageUpdate,
                schema.IdleSessionStateUpdate,
            ]
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

        second = AcpProcess(tmp_path, model)
        loader = AcpClient()
        conn = await second.start(loader, registration)
        try:
            await handshake(conn)
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[],
                replay_from=schema.ReplayFromStartVariant(),
            )
            assert [t.title for t in loader.of(schema.SessionInfoUpdate)] == [
                self.TITLE
            ]
            assert len(loader.of(schema.UsageUpdate)) == 1
            kinds = update_kinds(second.frames, "session/resume")
            assert kinds[-2:] == ["usage_update", "session_info_update"]
            assert kinds[0] == "user_message"
        finally:
            await conn.close()
            await second.close_stdin()
            await second.kill()

    async def test_compaction_reports_compaction_and_usage(
        self, registration: StandRegistration, tmp_path: Path
    ) -> None:
        counting = FakeModel(estimate_usage=True)
        await counting.start()
        boba = AcpProcess(tmp_path, counting)
        client = AcpClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        document["models"]["fake"] = boba.profile_document(
            "fake-model", context_window=8000, max_output_tokens=512, chars_per_token=4
        )
        try:
            conn = await boba.start(client, registration, document)
            try:
                await handshake(conn)
                session = await conn.new_session(
                    cwd=str(tmp_path), mcp_servers=[StandRegistration.STAND]
                )
                session_id = session.session_id
                large = {"name": "mcp__stand__large", "arguments": {"size": 24000}}
                _, first = await boba.turn(client, session_id, call(large))
                assert first.stop_reason == StopReason.END_TURN
                assert len(client.of(schema.UsageUpdate)) == 1

                _, second = await boba.turn(
                    client,
                    session_id,
                    [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
                )
                assert second.stop_reason == StopReason.END_TURN
                usage = client.of(schema.UsageUpdate)
                assert len(usage) == 3
                assert usage[1].used < usage[0].used
                assert all(u.size == 8000 - 512 for u in usage)
                compactions = client.of(schema.SessionCompactionUpdate)
                assert len(compactions) == 1
                assert compactions[0].status == "completed"
                assert compactions[0].compaction_id
            finally:
                await conn.close()
                await boba.close_stdin()

            assert await boba.wait() == 0
        finally:
            await boba.kill()
            await counting.stop()

        records = boba.journal(session_id)
        boundaries = [
            r
            for r in records
            if r["type"] == "system" and r.get("subtype") == "compact_boundary"
        ]
        assert len(boundaries) == 1
        assert boundaries[0]["uuid"] == compactions[0].compaction_id
        assert any(r["type"] == "user" and r.get("isCompactSummary") for r in records)


class TestFork:
    """Ветвление сессии хранилища: срез по `messageId` вопроса, копия
    целиком, пустая ветка, отказы."""

    PROMPTS: ClassVar[tuple[str, ...]] = TestHistory.PROMPTS
    REPLAY: ClassVar[schema.ReplayFromStartVariant] = schema.ReplayFromStartVariant()

    @staticmethod
    def questions(rendered: Sequence[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        return [item for item in rendered if item[0] == "user"]

    @staticmethod
    def human(journal: Sequence[Document]) -> list[str]:
        found: list[str] = []
        for record in journal:
            if record["type"] != "user":
                continue

            if record.get("origin", {}).get("kind") != "human":
                continue

            found.append(record["message"]["content"])

        return found

    async def test_fork_before_the_second_question_keeps_only_the_first_turn(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        session_id, _ = await TestHistory().conversation(boba, registration, tmp_path)
        source_before = boba.journal(session_id)

        second = AcpProcess(tmp_path, model)
        client = AcpClient()
        document = second.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await second.start(client, registration, document)
        try:
            await handshake(conn)
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[StandRegistration.STAND],
                replay_from=self.REPLAY,
            )
            view = FeedView()
            original = view.render(client.updates_of(session_id))
            questions = self.questions(original)
            assert [q[2] for q in questions] == list(self.PROMPTS)
            second_question = questions[1]

            meta: dict[str, Any] = {MetaKey.UP_TO_MESSAGE_ID.value: second_question[1]}
            forked = await conn.fork_session(
                session_id=session_id,
                cwd=str(tmp_path),
                mcp_servers=[StandRegistration.STAND],
                **meta,
            )
            branch_id = forked.session_id
            assert branch_id != session_id
            assert client.updates_of(branch_id) == []
            assert option_of(forked, ConfigOptionId.MODE).current_value == (
                "bypassPermissions"
            )
            assert option_of(forked, ConfigOptionId.MODEL).current_value == "fake"

            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=branch_id,
                mcp_servers=[StandRegistration.STAND],
                replay_from=self.REPLAY,
            )
            branch = view.render(client.updates_of(branch_id))
            assert branch == original[: original.index(second_question)]
            assert view.user_texts(branch) == [self.PROMPTS[0]]
            assert len(view.without_user(branch)) >= 3

            _, branch_idle = await second.turn(
                client,
                branch_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert branch_idle.stop_reason == StopReason.END_TURN

            _, again = await second.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert again.stop_reason == StopReason.END_TURN
        finally:
            await conn.close()
            await second.close_stdin()
            assert await second.wait() == 0

        cut = [r.get("uuid") for r in source_before].index(second_question[1])
        prefix = [{**r, "sessionId": branch_id} for r in source_before[:cut]]
        branch_journal = second.journal(branch_id)
        assert branch_journal[:cut] == prefix
        assert [r["seq"] for r in branch_journal[:cut]] == list(range(1, cut + 1))
        assert branch_journal[cut]["seq"] == cut + 1
        assert self.human(branch_journal) == [
            self.PROMPTS[0],
            ScenarioName.ANSWER.value,
        ]

        source_after = second.journal(session_id)
        assert source_after[: len(source_before)] == source_before
        assert self.human(source_after) == [*self.PROMPTS, ScenarioName.ANSWER.value]

    async def test_fork_without_a_cut_copies_everything_after_a_flush(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        session_id, _ = await TestHistory().conversation(boba, registration, tmp_path)

        second = AcpProcess(tmp_path, model)
        client = AcpClient()
        document = second.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await second.start(client, registration, document)
        try:
            await handshake(conn)
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[StandRegistration.STAND],
            )
            _, idle = await second.turn(
                client,
                session_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert idle.stop_reason == StopReason.END_TURN

            forked = await conn.fork_session(
                session_id=session_id,
                cwd=str(tmp_path),
                mcp_servers=[StandRegistration.STAND],
            )
            branch_id = forked.session_id
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=branch_id,
                mcp_servers=[StandRegistration.STAND],
                replay_from=self.REPLAY,
            )
            branch = FeedView().render(client.updates_of(branch_id))
            assert FeedView().user_texts(branch) == [
                *self.PROMPTS,
                ScenarioName.ANSWER.value,
            ]
            await conn.close_session(session_id=session_id)
            await conn.close_session(session_id=branch_id)
        finally:
            await conn.close()
            await second.close_stdin()
            assert await second.wait() == 0

        source = second.journal(session_id)
        branch_journal = second.journal(branch_id)
        assert branch_journal == [{**r, "sessionId": branch_id} for r in source]

    async def test_fork_at_the_first_question_is_an_empty_session(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        session_id, _ = await TestHistory().conversation(boba, registration, tmp_path)

        second = AcpProcess(tmp_path, model)
        client = AcpClient()
        document = second.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await second.start(client, registration, document)
        try:
            await handshake(conn)
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=session_id,
                mcp_servers=[StandRegistration.STAND],
                replay_from=self.REPLAY,
            )
            first_question = self.questions(
                FeedView().render(client.updates_of(session_id))
            )[0]

            meta: dict[str, Any] = {MetaKey.UP_TO_MESSAGE_ID.value: first_question[1]}
            forked = await conn.fork_session(
                session_id=session_id,
                cwd=str(tmp_path),
                mcp_servers=[],
                **meta,
            )
            branch_id = forked.session_id
            await conn.resume_session(
                cwd=str(tmp_path),
                session_id=branch_id,
                mcp_servers=[],
                replay_from=self.REPLAY,
            )
            assert FeedView().render(client.updates_of(branch_id)) == []
            assert [type(u) for u in client.updates_of(branch_id)] == [
                schema.UsageUpdate
            ]

            _, idle = await second.turn(
                client,
                branch_id,
                [schema.TextContentBlock(text=ScenarioName.ANSWER.value)],
            )
            assert idle.stop_reason == StopReason.END_TURN
        finally:
            await conn.close()
            await second.close_stdin()
            assert await second.wait() == 0

        branch_journal = second.journal(branch_id)
        assert branch_journal[0]["seq"] == 1
        assert self.human(branch_journal) == [ScenarioName.ANSWER.value]

    async def test_refusals(
        self,
        boba: AcpProcess,
        registration: StandRegistration,
        model: FakeModel,
        tmp_path: Path,
    ) -> None:
        session_id, _ = await TestHistory().conversation(boba, registration, tmp_path)
        journals = sorted((tmp_path / "history").glob("*.jsonl"))

        second = AcpProcess(tmp_path, model)
        client = AcpClient()
        conn = await second.start(client, registration)
        try:
            await handshake(conn)
            for session in ("no-such", "bad/id"):
                with pytest.raises(RequestError) as unknown:
                    await conn.fork_session(
                        session_id=session, cwd=str(tmp_path), mcp_servers=[]
                    )

                assert unknown.value.code == AcpErrorCode.RESOURCE_NOT_FOUND

            meta: dict[str, Any] = {MetaKey.UP_TO_MESSAGE_ID.value: "nope"}
            with pytest.raises(RequestError) as no_record:
                await conn.fork_session(
                    session_id=session_id,
                    cwd=str(tmp_path),
                    mcp_servers=[],
                    **meta,
                )

            assert no_record.value.code == AcpErrorCode.INVALID_PARAMS
            assert isinstance(no_record.value.data, dict)
            assert no_record.value.data["upToMessageId"] == "nope"
            assert no_record.value.data["sessionId"] == session_id

            meta = {MetaKey.UP_TO_MESSAGE_ID.value: ""}
            with pytest.raises(RequestError) as blank:
                await conn.fork_session(
                    session_id=session_id,
                    cwd=str(tmp_path),
                    mcp_servers=[],
                    **meta,
                )

            assert blank.value.code == AcpErrorCode.INVALID_PARAMS

            with pytest.raises(RequestError) as relative:
                await conn.fork_session(
                    session_id=session_id, cwd="relative", mcp_servers=[]
                )

            assert relative.value.code == AcpErrorCode.INVALID_PARAMS
        finally:
            await conn.close()
            await second.close_stdin()
            assert await second.wait() == 0

        assert sorted((tmp_path / "history").glob("*.jsonl")) == journals
