"""Вход ACP v2: агент `boba acp` над ядром boba-agent на SDK acp.experimental.v2.

Сервер JSON-RPC по stdio на стороне агента AgentSide (boba-transport-acp):
`initialize` с `capabilities.session.mcp.acp`, `session/new`,
`session/prompt`, `session/cancel`, запрос `session/request_permission` к
клиенту; история — `session/resume` (с `replayFrom: start` беседа
воспроизводится уведомлениями до ответа), `session/list` страницами с
курсором, `session/close`, `session/delete`, `session/fork`. Сессию открывает
сборка (SessionAssembly): агент ядра над инструментами серверов, которые
клиент объявил записями `type: "acp"`; инструменты агент читает и зовёт
запросами MCP внутри `mcp/message` (AcpToolServer). События ядра приёмник
SessionRelay переводит в уведомления по таблице AcpMapping.

Ход по v2: ответ на `session/prompt` — `messageId` вставленного вопроса (он же
uuid записи `user`), ход описывают `state_update`: `running` на первом
событии хода и `idle` со `stopReason` в конце. Сбой после вставки вопроса
кончает ход `idle` с `stopReason: "error"`, а не ошибкой ответа. Второй
`prompt` в идущем ходе встаёт в очередь ядра; `session/cancel` прерывает ход
и очищает очередь.

Сессию, которую держит другой процесс, `resume` открывает наблюдателем
(ObserverSession): историю он отдаёт, а `prompt` отвечает -32603 «held by
another process», пока замок занят; когда держатель вышел, следующий
`prompt` открывает сессию по-настоящему.

Настройки сессии — `configOptions` `mode` (режимы разрешений ядра) и `model`
(профили `models.<имя>` настроек) в ответах `session/new|resume|fork`;
`session/set_config_option` меняет их, после смены уходит
`config_option_update`. После хода и сжатия уходит `usage_update`, при сжатии
— `compaction_update`, после первого хода и при воспроизведении —
`session_info_update` с заголовком; ошибка вне хода уходит `notice`. Точка
среза `session/fork` — расширение `_meta["boba/upToMessageId"]`.

Ошибки:
RequestError — ошибка JSON-RPC клиенту: параметры запроса (-32602),
    неизвестная сессия (-32002), сборка, хранилище, серверы MCP или сессия у
    другого процесса (-32603).
SessionRefusedError — сборка сессии отвергла параметры запроса; поднимает
    реализация SessionAssembly, вход переводит в -32602.
SessionOpenError — сессию открыть не удалось: хранилище, серверы MCP,
    профиль; поднимает реализация SessionAssembly и AcpToolServer, вход
    переводит в -32603.
SessionBusyError — модель меняют при идущем ходе; поднимает реализация
    OpenedSession, вход переводит в -32603.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeAlias

import mcp_types as mt
from acp.exceptions import RequestError
from acp.experimental.v2 import Agent as AcpAgentProtocol
from acp.experimental.v2 import AgentSideConnection, schema
from mcp_types.methods import parse_server_notification, parse_server_result
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
from boba.agent.history import (
    HistoryError,
    RecordNotFoundError,
    SessionHeldError,
    SessionId,
    SessionNotFoundError,
)
from boba.agent.ids import IdMint
from boba.agent.permissions import PermissionModeError
from boba.agent.queue import QueueEntry, QueueKind, QueuePriority, QueueRefusedError
from boba.agent.records import (
    AssistantRecord,
    ChainRecord,
    CompactBoundaryRecord,
    PermissionMode,
    Record,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserRecord,
)
from boba.agent.session import SessionState
from boba.agent.settings import (
    McpClientJson,
)
from boba.agent.turn import ContextUsage
from boba.cancellation import StopReason as CoreStopReason
from boba.toolkit.dag import (
    DagNode,
    NodeOutcome,
    NodeOutcomes,
    ToolCard,
    ToolHints,
    ToolServer,
    ToolServers,
    ToolServersError,
    ToolUseResults,
)
from boba.toolkit.mcp import (
    McpFailure,
    McpResults,
    McpToolNameError,
    McpToolNames,
    McpToolStubs,
)
from boba.toolkit.result import ErrorResult, FailureResult, ToolResultBase
from boba.transport.acp.protocol import (
    AcpErrorCode,
    AcpTransportError,
    AcpVersion,
    AgentSide,
    ErrorDataKey,
    IdleErrorNotification,
    IdleErrorUpdate,
    McpErrorOutcome,
    McpMessage,
    McpVersion,
    MetaKey,
    StopReason,
    ToolStatus,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AcpAgent",
    "AcpMapping",
    "AcpToolServer",
    "CallProgressRelay",
    "ClientLink",
    "ConfigOptionId",
    "ForkMeta",
    "McpServerSpecs",
    "ObservedSession",
    "OpenedSession",
    "PermissionChoice",
    "PromptText",
    "SessionAssembly",
    "SessionBusyError",
    "SessionOpenError",
    "SessionOptions",
    "SessionPage",
    "SessionRefusedError",
    "SessionRelay",
    "SessionSettings",
    "SessionSummary",
    "SessionTitles",
    "ToolHintSource",
    "ToolKindName",
]

ContentBlock: TypeAlias = (
    schema.TextContentBlock
    | schema.ImageContentBlock
    | schema.AudioContentBlock
    | schema.ResourceContentBlock
    | schema.EmbeddedResourceContentBlock
    | schema.OtherContentBlock
)
McpServerBlock: TypeAlias = (
    schema.HttpMcpServer
    | schema.AcpMcpServer
    | schema.StdioMcpServer
    | schema.OtherMcpServer
)
ConfigOption: TypeAlias = (
    schema.SelectSessionConfigOption
    | schema.BooleanSessionConfigOption
    | schema.OtherSessionConfigOption
)
Update: TypeAlias = (
    schema.UserMessageUpdate
    | schema.AgentMessageUpdate
    | schema.AgentThoughtUpdate
    | schema.AgentMessageChunk
    | schema.AgentThoughtChunk
    | schema.SessionToolCallUpdate
    | schema.UsageUpdate
    | schema.ConfigOptionUpdate
    | schema.SessionInfoUpdate
    | schema.SessionCompactionUpdate
    | schema.SessionNotice
    | schema.RunningSessionStateUpdate
    | schema.IdleSessionStateUpdate
)


class SessionRefusedError(Exception):
    """Сборка сессии отвергла параметры запроса."""


class SessionOpenError(Exception):
    """Сессию открыть не удалось: хранилище, сервер MCP, профиль."""


class SessionBusyError(Exception):
    """Ход идёт: модель сессии менять поздно."""


class ConfigOptionId(StrEnum):
    """Настройки сессии (`configOptions`): идентификатор совпадает с
    категорией ACP того же имени."""

    MODE = "mode"
    MODEL = "model"


class ToolKindName(StrEnum):
    """Виды инструментов ACP (`kind`), которые агент выводит из пометок."""

    READ = "read"
    EDIT = "edit"
    EXECUTE = "execute"
    OTHER = "other"


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


class ForkMeta(BaseModel):
    """Расширение `boba/upToMessageId` в `_meta` запроса `session/fork`:
    `messageId` вопроса (uuid его записи `user`), на котором ветка кончается —
    в ветку идут записи до него. Без ключа ветка — копия всей сессии. Чужие
    ключи `_meta` не разбираются."""

    model_config = ConfigDict(extra="ignore")

    up_to_message_id: str | None = Field(
        default=None, alias=MetaKey.UP_TO_MESSAGE_ID.value, min_length=1
    )


@dataclass(frozen=True)
class SessionSummary:
    """Сессия хранилища для `session/list`: рабочий каталог, первый вопрос
    человека (None — вопросов не было) и время последней записи."""

    session_id: str
    cwd: str
    first_prompt: str | None
    updated_at: str


@dataclass(frozen=True)
class SessionPage:
    """Страница сессий хранилища и курсор следующей; None — страница последняя."""

    sessions: Sequence[SessionSummary]
    next_cursor: str | None


class SessionTitles:
    """Заголовок сессии для клиента: первая строка первого вопроса, не
    длиннее предела."""

    LIMIT: ClassVar[int] = 80
    ELLIPSIS: ClassVar[str] = "…"

    def title(self, first_prompt: str | None) -> str | None:
        if first_prompt is None:
            return None

        line = ""
        for candidate in first_prompt.splitlines():
            stripped = candidate.strip()
            if stripped:
                line = stripped
                break

        if not line:
            return None

        if len(line) > self.LIMIT:
            return line[: self.LIMIT] + self.ELLIPSIS

        return line


@dataclass(frozen=True)
class SessionSettings:
    """Состояние настроек сессии: действующий режим разрешений и доступные
    режимы, имя действующего профиля модели и имена доступных профилей."""

    mode: PermissionMode
    modes: Sequence[PermissionMode]
    model: str
    models: Sequence[str]

    def __post_init__(self) -> None:
        if self.mode not in self.modes:
            msg = f"the current mode {self.mode.value!r} is not among the available"
            raise ValueError(msg)

        if self.model not in self.models:
            msg = f"the current model {self.model!r} is not among the available"
            raise ValueError(msg)


@dataclass(frozen=True)
class ModeCard:
    """Имя и описание режима разрешений для клиента."""

    name: str
    description: str


class SessionOptions:
    """Настройки сессии в форме ACP: `configOptions` `mode` и `model` из одного
    состояния SessionSettings; то же состояние — уведомлением
    `config_option_update`. Создаёт вход; ответы `session/new|resume|fork`
    собирают через него."""

    CARDS: ClassVar[Mapping[PermissionMode, ModeCard]] = {
        PermissionMode.DEFAULT: ModeCard(
            "Default", "Asks before risky actions; read-only tools run freely"
        ),
        PermissionMode.ACCEPT_EDITS: ModeCard(
            "Accept edits", "Edits run without asking; destructive actions still ask"
        ),
        PermissionMode.PLAN: ModeCard("Plan", "Tools that change anything do not run"),
        PermissionMode.DONT_ASK: ModeCard(
            "Don't ask", "Anything not allowed in advance is rejected"
        ),
        PermissionMode.BYPASS_PERMISSIONS: ModeCard(
            "Bypass permissions", "No permission checks; everything runs"
        ),
        PermissionMode.AUTO: ModeCard(
            "Auto", "A classifier model answers permission questions"
        ),
    }

    def config_options(self, settings: SessionSettings) -> list[ConfigOption]:
        return [self._mode_option(settings), self._model_option(settings)]

    def options_update(self, settings: SessionSettings) -> schema.ConfigOptionUpdate:
        return schema.ConfigOptionUpdate(config_options=self.config_options(settings))

    def parse_mode(self, value: str, settings: SessionSettings) -> PermissionMode:
        """Ошибки:
        RequestError — такого режима нет или он недоступен при параметрах
            запуска (-32602).
        """
        try:
            mode = PermissionMode(value)
        except ValueError as exc:
            raise self._refused(value, ConfigOptionId.MODE, settings.modes) from exc

        if mode not in settings.modes:
            raise self._refused(value, ConfigOptionId.MODE, settings.modes)

        return mode

    def parse_model(self, value: str, settings: SessionSettings) -> str:
        """Ошибки:
        RequestError — профиля с таким именем нет в настройках (-32602).
        """
        if value not in settings.models:
            raise self._refused(value, ConfigOptionId.MODEL, settings.models)

        return value

    def _mode_option(
        self, settings: SessionSettings
    ) -> schema.SelectSessionConfigOption:
        options: list[schema.SessionConfigSelectOption] = []
        for mode in settings.modes:
            card = self.CARDS[mode]
            options.append(
                schema.SessionConfigSelectOption(
                    value=mode.value, name=card.name, description=card.description
                )
            )

        return schema.SelectSessionConfigOption(
            config_id=ConfigOptionId.MODE,
            name="Permission mode",
            description="How the agent treats tool calls that change something",
            category=ConfigOptionId.MODE,
            current_value=settings.mode.value,
            options=options,
        )

    def _model_option(
        self, settings: SessionSettings
    ) -> schema.SelectSessionConfigOption:
        options: list[schema.SessionConfigSelectOption] = []
        for name in settings.models:
            options.append(schema.SessionConfigSelectOption(value=name, name=name))

        return schema.SelectSessionConfigOption(
            config_id=ConfigOptionId.MODEL,
            name="Model",
            description="Model profile from the agent settings used for the next turns",
            category=ConfigOptionId.MODEL,
            current_value=settings.model,
            options=options,
        )

    @staticmethod
    def _refused(
        value: str, option_id: ConfigOptionId, known: Sequence[object]
    ) -> RequestError:
        names: list[str] = []
        for item in known:
            names.append(str(item))

        details = (
            f"configOption '{option_id.value}' has no value {value!r}; "
            f"available: {names}"
        )

        return RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})


class ToolHintSource(Protocol):
    """Откуда берутся пометки инструмента по имени: агент ядра или
    наблюдатель с подключёнными серверами."""

    @abstractmethod
    def tool_hints(self, name: str) -> ToolHints | None: ...


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
    """Запрос разрешения клиенту: заголовок, варианты ответа и вызов."""

    model_config = ConfigDict(frozen=True)

    title: str
    options: Sequence[schema.PermissionOption]
    subject: schema.ToolCallPermissionSubjectVariant


class AcpMapping:
    """Соответствие ядра протоколу: исходы хода → `idle` со `stopReason`,
    события → уведомления `session/update`, пометки инструмента → `kind`,
    вопрос `can_use_tool` → запрос разрешения и ответ на него.

    Единственное место, где ядро встречается со схемой ACP; создаёт вход и
    отдаёт приёмникам событий сессий.
    """

    STOP_REASONS: ClassVar[Mapping[TurnOutcome, StopReason]] = {
        TurnOutcome.COMPLETED: StopReason.END_TURN,
        TurnOutcome.MAX_TURNS: StopReason.MAX_TURN_REQUESTS,
        TurnOutcome.OUTPUT_LIMIT: StopReason.MAX_TOKENS,
        TurnOutcome.ABORTED_STREAMING: StopReason.CANCELLED,
        TurnOutcome.ABORTED_TOOLS: StopReason.CANCELLED,
        TurnOutcome.SHUTDOWN: StopReason.CANCELLED,
        TurnOutcome.BLOCKING_LIMIT: StopReason.REFUSAL,
        TurnOutcome.COMPACT_FAILURE_BREAKER: StopReason.REFUSAL,
        TurnOutcome.RAPID_REFILL_BREAKER: StopReason.REFUSAL,
    }
    COMPACTED: ClassVar[str] = "completed"

    def __init__(self) -> None:
        self._titles = ToolTitles()
        self._uses = ToolUseResults()

    def idle(
        self, result: ResultEvent
    ) -> schema.IdleSessionStateUpdate | IdleErrorUpdate:
        """Конец хода по его итогу; отказ модели — idle с ошибкой."""
        found = self.STOP_REASONS.get(result.outcome)
        if found is None:
            return self.idle_error(result.outcome.value, result.error)

        return schema.IdleSessionStateUpdate(stop_reason=found)

    def idle_error(self, kind: str, message: str) -> IdleErrorUpdate:
        """Конец хода сбоем: вид сбоя в `data`, текст — сообщение ошибки."""
        error = schema.Error(
            code=AcpErrorCode.INTERNAL_ERROR,
            message=message,
            data={ErrorDataKey.KIND.value: kind},
        )

        return IdleErrorUpdate(stop_reason=StopReason.ERROR, error=error)

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
            updates.append(
                schema.AgentThoughtChunk(
                    message_id=event.message_id,
                    content=schema.TextContentBlock(text=event.reasoning),
                )
            )

        if event.content:
            updates.append(
                schema.AgentMessageChunk(
                    message_id=event.message_id,
                    content=schema.TextContentBlock(text=event.content),
                )
            )

        return updates

    def tool_call(
        self, block: ToolUseBlock, hints: ToolHints | None
    ) -> schema.SessionToolCallUpdate:
        return schema.SessionToolCallUpdate(
            tool_call_id=block.id,
            name=block.name,
            title=self._titles.title(block.name, block.input),
            kind=self.tool_kind(hints),
            status=ToolStatus.PENDING,
            raw_input=dict(block.input),
        )

    def tool_results(
        self, record: UserRecord
    ) -> Sequence[schema.SessionToolCallUpdate]:
        updates: list[schema.SessionToolCallUpdate] = []
        for block in record.message.tool_results():
            updates.append(self._tool_result(block, record.tool_use_result))

        return updates

    def user_updates(self, record: UserRecord) -> Iterator[Update]:
        """Запись `user`: итоги вызовов либо вопрос человека целым
        сообщением `user_message` с uuid записи как `messageId` — текст, за
        ним ссылки на вложения блоками resource_link; служебные записи в
        ленту не идут."""
        results = self.tool_results(record)
        if results:
            yield from results
            return

        text = record.human_text()
        if text is None:
            return

        content: list[ContentBlock] = [schema.TextContentBlock(text=text)]
        for link in record.message.links():
            dumped = link.model_dump(mode="json", by_alias=True, exclude_none=True)
            content.append(schema.ResourceContentBlock.model_validate(dumped))

        yield schema.UserMessageUpdate(message_id=record.uuid, content=content)

    def replay(
        self, conversation: Sequence[ChainRecord], hints: ToolHintSource
    ) -> Sequence[Update]:
        """Беседа из записей → уведомления по порядку записей: сообщения
        целиком (`user_message`, `agent_thought`, `agent_message`), вызовы и
        их итоги — как в живом ходе. Синтетические записи агента (заметки об
        ошибках, заглушки) не воспроизводятся, оборванный ответ —
        воспроизводится; служебные сообщения, вложения и границы сжатия в
        ленту не идут."""
        updates: list[Update] = []
        for record in conversation:
            updates.extend(self._replayed(record, hints))

        return updates

    def _replayed(self, record: ChainRecord, hints: ToolHintSource) -> Iterator[Update]:
        if isinstance(record, UserRecord):
            yield from self.user_updates(record)
            return

        if isinstance(record, AssistantRecord):
            yield from self._replayed_assistant(record, hints)

    def _replayed_assistant(
        self, record: AssistantRecord, hints: ToolHintSource
    ) -> Iterator[Update]:
        if record.is_synthetic() and not record.is_aborted_mid_stream:
            return

        message_id = record.message.id
        thoughts: list[ContentBlock] = []
        texts: list[ContentBlock] = []
        calls: list[ToolUseBlock] = []
        for block in record.message.content:
            if isinstance(block, ThinkingBlock):
                thoughts.append(schema.TextContentBlock(text=block.thinking))
                continue

            if isinstance(block, TextBlock):
                texts.append(schema.TextContentBlock(text=block.text))
                continue

            calls.append(block)

        if thoughts:
            yield schema.AgentThoughtUpdate(message_id=message_id, content=thoughts)

        if texts:
            yield schema.AgentMessageUpdate(message_id=message_id, content=texts)

        for block in calls:
            yield self.tool_call(block, hints.tool_hints(block.name))

    def _tool_result(
        self, block: ToolResultBlock, stored: Any
    ) -> schema.SessionToolCallUpdate:
        """Итог вызова: текст модели содержимым, ответ сервера как пришёл —
        `rawOutput`."""
        status = ToolStatus.COMPLETED
        if block.is_error:
            status = ToolStatus.FAILED

        text = schema.TextContentBlock(text=block.text())

        return schema.SessionToolCallUpdate(
            tool_call_id=block.tool_use_id,
            status=status,
            content=[schema.ContentToolCallContent(content=text)],
            raw_output=self._uses.of(stored).artifact,
        )

    def tool_started(self, event: ToolStartedEvent) -> schema.SessionToolCallUpdate:
        return schema.SessionToolCallUpdate(
            tool_call_id=event.tool_use_id, status=ToolStatus.IN_PROGRESS
        )

    def tool_progress(self, event: ToolProgressEvent) -> schema.SessionToolCallUpdate:
        """Ход вызова — одна строка содержимого, которая заменяет прежнюю, и
        `_meta` уведомления сервера как есть. Числа без итога ничего не
        говорят: у отчёта с текстом без total строка — сам текст."""
        done = f"{event.progress:g}"
        if event.total is not None:
            done = f"{done} of {event.total:g}"

        if not event.message:
            line = done
        elif event.total is None:
            line = event.message
        else:
            line = f"{event.message} — {done}"

        text = schema.TextContentBlock(text=line)

        meta: dict[str, Any] | None = None
        if event.meta:
            meta = dict(event.meta)

        return schema.SessionToolCallUpdate(
            tool_call_id=event.tool_use_id,
            status=ToolStatus.IN_PROGRESS,
            content=[schema.ContentToolCallContent(content=text)],
            field_meta=meta,
        )

    def usage_update(self, usage: ContextUsage) -> schema.UsageUpdate:
        """Заполненность контекста: `used` — оценка разговора, `size` —
        действующее окно профиля."""
        return schema.UsageUpdate(used=usage.used, size=usage.size)

    def title_update(self, title: str) -> schema.SessionInfoUpdate:
        return schema.SessionInfoUpdate(title=title)

    def compaction(
        self, record: CompactBoundaryRecord
    ) -> schema.SessionCompactionUpdate:
        return schema.SessionCompactionUpdate(
            compaction_id=record.uuid, status=self.COMPACTED
        )

    def notice(self, event: ErrorEvent) -> schema.SessionNotice:
        return schema.SessionNotice(severity="error", title=event.message)

    def permission_request(
        self, request: CanUseToolRequest, hints: ToolHints | None
    ) -> PermissionAsk:
        title = self._titles.title(request.tool_name, request.input)
        tool_call = schema.ToolCallUpdate(
            tool_call_id=request.tool_use_id,
            name=request.tool_name,
            title=title,
            kind=self.tool_kind(hints),
            status=ToolStatus.PENDING,
            raw_input=dict(request.input),
        )
        options: list[schema.PermissionOption] = []
        for choice in PermissionChoice:
            options.append(
                schema.PermissionOption(
                    option_id=choice.value, name=choice.label, kind=choice
                )
            )

        return PermissionAsk(
            title=title,
            options=options,
            subject=schema.ToolCallPermissionSubjectVariant(tool_call=tool_call),
        )

    def permission_answer(
        self, response: schema.RequestPermissionResponse, request: CanUseToolRequest
    ) -> PermissionAnswer:
        """Ответ клиента → ответ ядру; отмена и неизвестный вариант — отказ."""
        outcome = response.outcome
        if not isinstance(outcome, schema.SelectedPermissionOutcomeVariant):
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
    """Вопрос из блоков `session/prompt` элементом очереди: текст как есть,
    ссылки на ресурсы — блоками resource_link записи `user`; прочие блоки
    агент не объявлял."""

    def render(self, blocks: Sequence[ContentBlock]) -> QueueEntry:
        """Ошибки:
        RequestError — блок неподдержанного вида или пустой вопрос (-32602).
        """
        parts: list[str] = []
        links: list[mt.ResourceLink] = []
        for block in blocks:
            if isinstance(block, schema.ResourceContentBlock):
                dumped = block.model_dump(mode="json", by_alias=True, exclude_none=True)
                links.append(mt.ResourceLink.model_validate(dumped))
                continue

            parts.append(self._part(block))

        text = "".join(parts)
        empty = not text.strip()
        if empty and not links:
            data = {ErrorDataKey.DETAILS.value: "the prompt has no text to send"}
            raise RequestError.invalid_params(data)

        return QueueEntry(kind=QueueKind.PROMPT, text=text, links=links)

    def _part(self, block: ContentBlock) -> str:
        if isinstance(block, schema.TextContentBlock):
            return block.text

        details = (
            f"prompt blocks of type '{block.type}' are not supported: this agent "
            "declared no image, audio or embedded context capability; send text "
            "and resource_link blocks"
        )
        raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})


class McpServerSpecs:
    """Серверы MCP из `session/new|resume|fork`: принимаются только записи
    `type: "acp"` (MCP-over-ACP). Имя сервера входит в полные имена его
    инструментов и проверяется их формой (McpToolNames)."""

    def __init__(self) -> None:
        self._names = McpToolNames()

    def servers(
        self, blocks: Sequence[McpServerBlock]
    ) -> Sequence[schema.AcpMcpServer]:
        """Ошибки:
        RequestError — запись не `acp`, повтор имени либо имя, которое не
            годится для полных имён инструментов (-32602).
        """
        accepted: list[schema.AcpMcpServer] = []
        named: set[str] = set()
        for server in blocks:
            if not isinstance(server, schema.AcpMcpServer):
                details = (
                    f"mcpServers entry of type '{server.type}' is refused: this "
                    "agent declared mcp.acp only and reaches tools through "
                    "mcp/message; declare the server with type 'acp'"
                )
                raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

            try:
                self._names.checked_server(server.name)
            except McpToolNameError as exc:
                details = f"mcpServers entry '{server.name}' is refused: {exc}"
                raise RequestError.invalid_params(
                    {ErrorDataKey.DETAILS.value: details}
                ) from exc

            if server.name in named:
                details = (
                    f"mcpServers names '{server.name}' twice; names must be unique"
                )
                raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

            named.add(server.name)
            accepted.append(server)

        return accepted


@dataclass(frozen=True)
class ClientLink:
    """Связь агента с клиентом: сторона агента (mcp/message, idle с ошибкой)
    и соединение SDK (уведомления и запрос разрешения). Создаёт
    AcpAgent.attach, держат SessionRelay и AcpToolServer."""

    side: AgentSide
    connection: AgentSideConnection


class CallProgressRelay:
    """Ход одного вызова: уведомление `notifications/progress` его запроса
    mcp/message → событие ToolProgressEvent ядра в приёмник сессии."""

    def __init__(self, sink: EventSink, tool_use_id: str) -> None:
        self._sink = sink
        self._tool_use_id = tool_use_id
        self._started = time.monotonic()

    async def accept(self, message: McpMessage) -> None:
        """Ошибки:
        RequestError — уведомление не разбирается как уведомление сервера MCP
            (-32602).
        """
        params: Mapping[str, Any] = {}
        if message.params is not None:
            params = message.params

        try:
            notification = parse_server_notification(
                message.method, McpVersion.V2026_07_28, params
            )
        except (KeyError, ValidationError) as exc:
            details = (
                f"call {self._tool_use_id}: mcp/message notification "
                f"{message.method!r} of request {message.request_id} expected an "
                f"MCP server notification, got {repr(params)[:200]}: {exc}"
            )
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

        if not isinstance(notification, mt.ProgressNotification):
            logger.info(
                "call %s: mcp/message notification %s is not progress; dropped",
                self._tool_use_id,
                message.method,
            )
            return

        progress = notification.params
        message_text = progress.message
        if message_text is None:
            message_text = ""

        meta: dict[str, Any] = {}
        if progress.meta is not None:
            meta = progress.meta

        await self._sink.emit(
            ToolProgressEvent(
                tool_use_id=self._tool_use_id,
                message=message_text,
                progress=progress.progress,
                total=progress.total,
                elapsed_sec=time.monotonic() - self._started,
                meta=meta,
            )
        )


class AcpToolServer(ToolServer):
    """Реализация ToolServer над одной регистрацией MCP-over-ACP: запись
    `type: "acp"` сессии. Инструменты — все страницы `tools/list`, вызов —
    `tools/call` запросом mcp/message через AgentSide; текст для модели
    собирает McpResults, ответ сервера как пришёл ложится в NodeOutcome.reply,
    ход вызова идёт в приёмник сессии через CallProgressRelay. Создаёт AcpAgent
    на каждую запись сессии и собирает их ToolServers."""

    def __init__(
        self,
        side: AgentSide,
        server: schema.AcpMcpServer,
        sink: EventSink,
        limits: McpClientJson,
    ) -> None:
        self._side = side
        self._server = server
        self._sink = sink
        self._limits = limits
        self._ids = IdMint()
        self._stubs = McpToolStubs()
        self._results = McpResults()
        self._names = McpToolNames()
        self._outcomes = NodeOutcomes()
        self._cards: list[ToolCard] = []
        self._meta: mt.RequestParamsMeta = {
            mt.PROTOCOL_VERSION_META_KEY: McpVersion.V2026_07_28.value,
            mt.CLIENT_CAPABILITIES_META_KEY: {},
        }

    async def open(self) -> None:
        """Карточки по всем страницам `tools/list`.

        Ошибки:
        SessionOpenError — клиент ответил ошибкой ACP, ошибкой MCP, не уложился
            в срок или ответил не списком инструментов.
        """
        cursor: str | None = None
        while True:
            page = await self._page(cursor)
            for tool in page.tools:
                try:
                    card = self._stubs.card(self._server.name, tool)
                except McpToolNameError as exc:
                    msg = (
                        f"MCP server '{self._server.name}' (serverId "
                        f"'{self._server.server_id}') lists a tool that is "
                        f"refused: {exc}"
                    )
                    raise SessionOpenError(msg) from exc

                self._cards.append(card)

            cursor = page.next_cursor
            if cursor is None:
                break

        logger.info(
            "mcp: server %s (%s) offers %d tool(s)",
            self._server.name,
            self._server.server_id,
            len(self._cards),
        )

    async def _page(self, cursor: str | None) -> mt.ListToolsResult:
        """Ошибки:
        SessionOpenError — страница не получена или не разобрана.
        """
        params = mt.PaginatedRequestParams(cursor=cursor, _meta=self._meta)
        request = mt.ListToolsRequest(params=params)
        message = McpMessage(
            server_id=self._server.server_id,
            request_id=self._ids.request_id(),
            **request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        what = (
            f"listing the tools of MCP server '{self._server.name}' "
            f"(serverId '{self._server.server_id}', cursor {cursor!r})"
        )
        try:
            async with asyncio.timeout(self._limits.list_timeout_sec):
                outcome = await self._side.request(message, None)
        except TimeoutError as exc:
            msg = (
                f"{what} expected an answer within "
                f"{self._limits.list_timeout_sec} s, got none"
            )
            raise SessionOpenError(msg) from exc
        except RequestError as exc:
            msg = (
                f"{what} expected a tools/list result, got ACP error {exc.code} "
                f"{exc}: {exc.data!r}"
            )
            raise SessionOpenError(msg) from exc
        except AcpTransportError as exc:
            msg = f"{what} expected a tools/list result, got no answer: {exc}"
            raise SessionOpenError(msg) from exc

        if isinstance(outcome, McpErrorOutcome):
            msg = (
                f"{what} expected a tools/list result, got MCP error "
                f"{outcome.error.code}: {outcome.error.message}"
            )
            raise SessionOpenError(msg)

        try:
            parsed = parse_server_result(
                request.method, McpVersion.V2026_07_28, outcome.result
            )
        except (ValidationError, KeyError, ValueError, TypeError) as exc:
            shown = repr(outcome.result)[:200]
            msg = f"{what} expected a tools/list result, got {shown}: {exc}"
            raise SessionOpenError(msg) from exc

        if not isinstance(parsed, mt.ListToolsResult):
            msg = f"{what} expected a tools/list result, got {type(parsed).__name__}"
            raise SessionOpenError(msg)

        return parsed

    def tools(self) -> Sequence[ToolCard]:
        return self._cards

    def features(self) -> Mapping[str, Mapping[str, object]]:
        return {}

    async def submit(
        self, calls: Sequence[DagNode]
    ) -> Sequence[asyncio.Future[NodeOutcome]]:
        pending: list[asyncio.Future[NodeOutcome]] = []
        for call in calls:
            pending.append(asyncio.ensure_future(self._call(call)))

        return pending

    async def _call(self, node: DagNode) -> NodeOutcome:
        """Один вызов `tools/call`; наружу идёт только отмена (контракт NodeBody)."""
        meta = mt.RequestParamsMeta(progress_token=node.key)
        meta.update(self._meta)
        params = mt.CallToolRequestParams(
            name=self._names.tool_of(node.tool), arguments=dict(node.args), _meta=meta
        )
        request = mt.CallToolRequest(params=params)
        message = McpMessage(
            server_id=self._server.server_id,
            request_id=self._ids.request_id(),
            **request.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
        relay = CallProgressRelay(self._sink, node.key)
        limit = self._limits.call_timeout_sec
        what = (
            f"calling tool {node.tool!r} (call {node.key}) on MCP server "
            f"'{self._server.name}' (serverId '{self._server.server_id}')"
        )
        try:
            async with asyncio.timeout(limit):
                outcome = await self._side.request(message, relay.accept)
        except TimeoutError:
            failure = ErrorResult(
                message=(
                    f"{what} expected a tools/call result within {limit} s, got none"
                ),
                error_kind=McpFailure.TRANSPORT,
            )
            return self._outcomes.refused(node, failure)
        except RequestError as exc:
            failure = ErrorResult(
                message=(
                    f"{what} expected a tools/call result, got ACP error {exc.code} "
                    f"{exc}: {exc.data!r}"
                ),
                error_kind=McpFailure.TRANSPORT,
            )
            return self._outcomes.refused(node, failure)
        except AcpTransportError as exc:
            failure = ErrorResult(
                message=f"{what} expected a tools/call result, got no answer: {exc}",
                error_kind=McpFailure.TRANSPORT,
            )
            return self._outcomes.refused(node, failure)

        if isinstance(outcome, McpErrorOutcome):
            error = self._results.error(node.tool, node.key, outcome.error)
            return self._outcomes.refused(node, error)

        shown = repr(outcome.result)[:200]
        try:
            parsed = parse_server_result(
                request.method, McpVersion.V2026_07_28, outcome.result
            )
        except (ValidationError, KeyError, ValueError) as exc:
            failure = ErrorResult(
                message=f"{what} expected a tools/call result, got {shown}: {exc}",
                error_kind=McpFailure.TRANSPORT,
            )
            return self._outcomes.refused(node, failure)

        family: ToolResultBase
        match parsed:
            case mt.CallToolResult() | mt.InputRequiredResult():
                family = self._results.result(node.tool, node.key, parsed)
            case _:
                family = ErrorResult(
                    message=(
                        f"{what} expected a tools/call result, got "
                        f"{type(parsed).__name__}: {shown}"
                    ),
                    error_kind=McpFailure.TRANSPORT,
                )

        if isinstance(family, FailureResult):
            built = self._outcomes.refused(node, family)
        else:
            built = self._outcomes.of(node, family, False)

        return built.model_copy(update={"reply": outcome.result})


class SessionRelay(EventSink):
    """Реализация EventSink сессии ACP: события ядра → уведомления клиенту.

    Создаёт вход на каждую сессию до сборки агента и отдаёт ядру
    приёмником; агент привязывается после открытия (`attach`), потому что
    пометки инструментов, заполненность контекста и ответы на вопросы живут
    у него. Первое событие хода шлёт `state_update` `running`, итог хода —
    `usage_update`, после первого вопроса — `session_info_update` и затем
    `idle`. Вопрос `can_use_tool` уходит клиенту отдельной задачей, которую
    снимает отмена хода. После ухода клиента приёмник отвязывается и дальше
    молчит.
    """

    def __init__(self, session_id: str, link: ClientLink, mapping: AcpMapping) -> None:
        self._session_id = session_id
        self._link = link
        self._mapping = mapping
        self._titles = SessionTitles()
        self._agent: Agent | None = None
        self._asks: dict[str, asyncio.Task[None]] = {}
        self._detached = False
        self._titled = False
        self._running = False

    def attach(self, agent: Agent) -> None:
        self._agent = agent

    async def replay(self, updates: Sequence[Update]) -> None:
        """Воспроизведение беседы: уведомления уходят по порядку тем же
        путём, что живые."""
        await self._send_all(updates)

    async def notify(self, update: Update) -> None:
        """Уведомление вне хода: смена настроек сессии."""
        await self._send(update)

    async def announce_loaded(self) -> None:
        """После воспроизведения сессии: заголовок и заполненность
        контекста, как после хода."""
        await self._send_all(self._turn_summary())

    async def failed(self, update: IdleErrorUpdate) -> None:
        """Ход кончился сбоем: `idle` с ошибкой."""
        self._running = False
        await self._send(update)

    async def emit(self, event: AgentEvent) -> None:
        if isinstance(event, ResultEvent):
            await self._send_all(self._turn_summary())
            idle = self._mapping.idle(event)
            if isinstance(idle, IdleErrorUpdate):
                await self.failed(idle)
                return

            self._running = False
            await self._send(idle)
            return

        if isinstance(event, ErrorEvent):
            logger.warning("session %s: %s", self._session_id, event.message)
            await self._send(self._mapping.notice(event))
            return

        updates = self._updates(event)
        if updates is None:
            self._control(event)
            return

        if not updates:
            return

        if not self._running:
            self._running = True
            await self._send(schema.RunningSessionStateUpdate())

        await self._send_all(updates)

    def _turn_summary(self) -> Sequence[Update]:
        """Уведомления по итогу хода: заполненность и, однажды, заголовок."""
        updates: list[Update] = []
        usage = self._usage()
        if usage is not None:
            updates.append(usage)

        title = self._title()
        if title is not None:
            updates.append(title)

        return updates

    def _usage(self) -> schema.UsageUpdate | None:
        if self._agent is None:
            return None

        return self._mapping.usage_update(self._agent.context_usage())

    def _title(self) -> schema.SessionInfoUpdate | None:
        """Заголовок из первого вопроса человека; уходит один раз."""
        if self._titled or self._agent is None:
            return None

        first = self._first_prompt(self._agent.session.conversation())
        title = self._titles.title(first)
        if title is None:
            return None

        self._titled = True

        return self._mapping.title_update(title)

    @staticmethod
    def _first_prompt(conversation: Sequence[ChainRecord]) -> str | None:
        for record in conversation:
            if not isinstance(record, UserRecord):
                continue

            text = record.human_text()
            if text is not None:
                return text

        return None

    def _updates(self, event: AgentEvent) -> Sequence[Update] | None:
        """Уведомления хода по событию; None — событие не про ленту."""
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
        if isinstance(event, ControlRequestEvent):
            self._ask(event)
            return

        if isinstance(event, ControlCancelEvent):
            self.withdraw(event.request_id)
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
            return list(self._mapping.user_updates(record))

        if isinstance(record, CompactBoundaryRecord):
            compaction: list[Update] = [self._mapping.compaction(record)]
            usage = self._usage()
            if usage is not None:
                compaction.append(usage)

            return compaction

        return ()

    def _hints(self, name: str) -> ToolHints | None:
        if self._agent is None:
            return None

        return self._agent.tool_hints(name)

    async def _send_all(self, updates: Sequence[Update]) -> None:
        for update in updates:
            await self._send(update)

    async def _send(self, update: Update | IdleErrorUpdate) -> None:
        if self._detached:
            return

        try:
            if isinstance(update, IdleErrorUpdate):
                notification = IdleErrorNotification(
                    session_id=self._session_id, update=update
                )
                await self._link.side.send_update(notification)
                return

            await self._link.connection.session_update(self._session_id, update)
        except (ConnectionError, AcpTransportError) as exc:
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
            response = await self._link.connection.request_permission(
                session_id=self._session_id,
                title=ask.title,
                options=list(ask.options),
                subject=ask.subject,
            )
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
    """Открытая сессия сборки: агент ядра, действующий профиль модели и всё,
    что надо закрыть вместе с ними."""

    @property
    @abstractmethod
    def agent(self) -> Agent: ...

    @property
    @abstractmethod
    def model(self) -> str:
        """Имя действующего профиля модели (`models.<имя>` настроек)."""

    @abstractmethod
    async def set_model(self, name: str) -> None:
        """Переключает профиль модели агента для следующих ходов.

        Ошибки:
        SessionRefusedError — профиля с таким именем нет.
        SessionBusyError — ход идёт.
        SessionOpenError — профиль или его провайдер не годятся.
        """

    @abstractmethod
    async def close(self) -> None:
        """Гасит ход и сбрасывает журнал."""


class ObservedSession(ToolHintSource, Protocol):
    """Сессия, открытая без замка: её держит другой процесс. Беседа
    прочитана из хранилища, инструменты подключены; `claim` открывает сессию
    по-настоящему, когда замок свободен."""

    @abstractmethod
    def conversation(self) -> Sequence[ChainRecord]: ...

    @abstractmethod
    async def claim(self) -> OpenedSession:
        """Ошибки:
        SessionHeldError — замок всё ещё у другого процесса.
        SessionNotFoundError — записи сессии исчезли из хранилища.
        SessionOpenError — сессию открыть не удалось.
        """


class SessionAssembly(Protocol):
    """Сборка сессии из настроек процесса и инструментов
    `session/new|resume|fork`, список и удаление сессий хранилища.

    Реализует вход процесса: он знает профиль модели и хранилище истории;
    протокольный слой знает только события, ответы и инструменты.
    """

    @abstractmethod
    async def open(
        self,
        session_id: SessionId,
        cwd: Path,
        tools: ToolServer | None,
        sink: EventSink,
        resume: bool,
    ) -> OpenedSession:
        """Ошибки:
        SessionOpenError — сессию открыть не удалось.
        SessionHeldError — при возобновлении: сессию держит другой процесс.
        SessionNotFoundError — при возобновлении: записей сессии нет.
        """

    @abstractmethod
    async def observe(
        self,
        session_id: SessionId,
        cwd: Path,
        tools: ToolServer | None,
        sink: EventSink,
    ) -> ObservedSession:
        """Ошибки:
        SessionNotFoundError — записей сессии нет.
        HistoryError — хранилище отказало при чтении.
        """

    @abstractmethod
    def models(self) -> Sequence[str]:
        """Имена профилей `models.<имя>` в порядке файла настроек."""

    @abstractmethod
    async def sessions(self, cursor: str | None) -> SessionPage:
        """Ошибки:
        HistoryError — хранилище отказало.
        """

    @abstractmethod
    async def delete(self, session_id: SessionId) -> None:
        """Ошибки:
        SessionHeldError — сессию держит работающий процесс.
        HistoryError — хранилище отказало.
        """

    @abstractmethod
    async def copy(
        self, source: SessionId, target: SessionId, up_to: str | None
    ) -> int:
        """Записи `source` до записи `up_to` (не включая; None — все) под
        идентификатором `target`; возвращает число скопированных записей.

        Ошибки:
        SessionNotFoundError — у источника нет записей.
        RecordNotFoundError — записи `up_to` в источнике нет.
        HistoryError — хранилище отказало.
        """


class AcpSession:
    """Одна сессия ACP: открытый агент, его обслуживание очереди и приёмник."""

    def __init__(
        self,
        session_id: str,
        opened: OpenedSession,
        relay: SessionRelay,
        mapping: AcpMapping,
    ) -> None:
        self._session_id = session_id
        self._opened = opened
        self._relay = relay
        self._mapping = mapping
        self._reports: list[asyncio.Task[None]] = []
        self._serving = asyncio.create_task(
            opened.agent.serve(), name=f"acp-serve-{session_id}"
        )
        self._serving.add_done_callback(self._served)

    @property
    def agent(self) -> Agent:
        return self._opened.agent

    @property
    def relay(self) -> SessionRelay:
        return self._relay

    @property
    def model(self) -> str:
        return self._opened.model

    def conversation(self) -> Sequence[ChainRecord]:
        return self.agent.session.conversation()

    def set_mode(self, mode: PermissionMode) -> None:
        """Режим разрешений записью ядра; действует со следующей проверки.

        Ошибки:
        RequestError — режим недоступен при параметрах запуска (-32602).
        """
        try:
            self.agent.set_permission_mode(mode)
        except PermissionModeError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    async def set_model(self, name: str) -> None:
        """Профиль модели для следующих ходов; при идущем ходе или непустой
        очереди — отказ.

        Ошибки:
        RequestError — ход идёт (-32603), профиля нет (-32602), профиль не
            годится (-32603).
        """
        if self._busy():
            details = (
                f"session {self._session_id} is busy: a turn is in progress; switch "
                "the model while the session is idle"
            )
            raise RequestError.internal_error({ErrorDataKey.DETAILS.value: details})

        try:
            await self._opened.set_model(name)
        except SessionRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc
        except (SessionBusyError, SessionOpenError) as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    def _busy(self) -> bool:
        if self.agent.session.state is not SessionState.IDLE:
            return True

        return len(self.agent.queue) > 0

    def _served(self, task: asyncio.Task[None]) -> None:
        """Обслуживание остановилось сбоем: идущий ход кончается `idle` с
        ошибкой."""
        if task.cancelled():
            return

        failure = task.exception()
        if failure is None:
            return

        logger.error(
            "session %s: the agent stopped serving: %s", self._session_id, failure
        )
        update = self._mapping.idle_error(type(failure).__name__, str(failure))
        self._reports.append(asyncio.create_task(self._relay.failed(update)))

    async def prompt(self, asked: QueueEntry) -> schema.PromptResponse:
        """Ставит вопрос в очередь ядра; `messageId` ответа — uuid записи
        `user`, которую напишет его ход.

        Ошибки:
        RequestError — очередь не приняла элемент (-32602), обслуживание
            сессии остановилось (-32603).
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

        entry = asked.model_copy(update={"priority": priority})
        try:
            item = agent.queue.enqueue(entry)
        except QueueRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        return schema.PromptResponse(message_id=item.id)

    async def flush(self) -> None:
        """Сбрасывает несброшенные записи сессии в хранилище.

        Ошибки:
        HistoryError — хранилище отказало.
        """
        await self._opened.agent.writer.flush(strict=True)

    def cancel(self, reason: CoreStopReason) -> None:
        """Прерывает ход и очищает очередь."""
        self.agent.interrupt(reason, cancel_queued=True)

    async def close(self) -> None:
        self._relay.withdraw_all()
        await self._opened.close()
        try:
            await self._serving
        except Exception as exc:
            logger.error(
                "session %s: serving ended with an error: %s", self._session_id, exc
            )

        await asyncio.gather(*self._reports)


class ObserverSession:
    """Сессия-наблюдатель: открыта без замка, беседу отдала, хода не ведёт.

    Создаёт вход на `session/resume`, когда сессию держит другой процесс;
    первый `prompt` зовёт `claim`, и при свободном замке наблюдатель уступает
    место обычной AcpSession с тем же приёмником.
    """

    def __init__(
        self,
        session_id: str,
        observed: ObservedSession,
        relay: SessionRelay,
        mapping: AcpMapping,
    ) -> None:
        self._session_id = session_id
        self._observed = observed
        self._relay = relay
        self._mapping = mapping

    @property
    def relay(self) -> SessionRelay:
        return self._relay

    def conversation(self) -> Sequence[ChainRecord]:
        return self._observed.conversation()

    @property
    def hints(self) -> ToolHintSource:
        return self._observed

    async def claim(self) -> AcpSession:
        """Ошибки:
        SessionHeldError — замок всё ещё у другого процесса.
        SessionNotFoundError — записи сессии исчезли из хранилища.
        SessionOpenError — сессию открыть не удалось.
        """
        opened = await self._observed.claim()
        self._relay.attach(opened.agent)

        return AcpSession(self._session_id, opened, self._relay, self._mapping)

    async def close(self) -> None:
        """Наблюдатель ничего не держит: закрывать нечего."""


HeldSession: TypeAlias = AcpSession | ObserverSession


class AcpAgent(AcpAgentProtocol):
    """Реализация Agent SDK v2 над сессиями ядра.

    Создаёт вход процесса со сборкой сессий; AgentSide зовёт `attach` и
    получает агента для роутера SDK. Один объект обслуживает все сессии
    соединения: каждая — свой агент ядра над инструментами серверов `acp` из
    `session/new|resume|fork`, либо наблюдатель, пока сессию держит другой
    процесс.
    """

    # SDK считает метод протокола неподдержанным
    login = AcpAgentProtocol.login
    logout = AcpAgentProtocol.logout
    list_providers = AcpAgentProtocol.list_providers
    set_provider = AcpAgentProtocol.set_provider
    disable_provider = AcpAgentProtocol.disable_provider
    mcp_message = AcpAgentProtocol.mcp_message
    notify_mcp = AcpAgentProtocol.notify_mcp
    start_nes = AcpAgentProtocol.start_nes
    suggest_nes = AcpAgentProtocol.suggest_nes
    accept_nes = AcpAgentProtocol.accept_nes
    reject_nes = AcpAgentProtocol.reject_nes
    close_nes = AcpAgentProtocol.close_nes
    did_open = AcpAgentProtocol.did_open
    did_change = AcpAgentProtocol.did_change
    did_close = AcpAgentProtocol.did_close
    did_save = AcpAgentProtocol.did_save
    did_focus = AcpAgentProtocol.did_focus

    def __init__(
        self,
        assembly: SessionAssembly,
        info: schema.Implementation,
        limits: McpClientJson,
    ) -> None:
        self._assembly = assembly
        self._info = info
        self._limits = limits
        self._link: ClientLink | None = None
        self._sessions: dict[str, HeldSession] = {}
        self._mapping = AcpMapping()
        self._options = SessionOptions()
        self._prompts = PromptText()
        self._specs = McpServerSpecs()
        self._titles = SessionTitles()
        self._ids = IdMint()

    def attach(self, side: AgentSide, connection: AgentSideConnection) -> AcpAgent:
        """Фабрика агента для AgentSide: связь с клиентом и сам агент."""
        self._link = ClientLink(side=side, connection=connection)

        return self

    def _linked(self) -> ClientLink:
        if self._link is None:
            msg = "The ACP agent has no client connection yet; attach() comes first."
            raise RuntimeError(msg)

        return self._link

    async def initialize(
        self,
        protocol_version: int,
        info: schema.Implementation,
        capabilities: schema.ClientCapabilities | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        logger.info(
            "initialize: %s %s speaks protocol version %d",
            info.name,
            info.version,
            protocol_version,
        )
        session = schema.SessionCapabilities(
            prompt=schema.PromptCapabilities(),
            mcp=schema.McpCapabilities(acp=schema.McpAcpCapabilities()),
            delete=schema.SessionDeleteCapabilities(),
            fork=schema.SessionForkCapabilities(
                field_meta={MetaKey.UP_TO_MESSAGE_ID.value: True}
            ),
        )

        return schema.InitializeResponse(
            protocol_version=AcpVersion.V2,
            info=self._info,
            capabilities=schema.AgentCapabilities(session=session),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        """Ошибки:
        RequestError — `cwd` не абсолютный или серверы MCP не годятся
            (-32602); инструменты или сессию открыть не удалось (-32603).
        """
        directory = self._directory(cwd)
        servers = self._specs.servers(self._blocks(mcp_servers))
        session_id = self._ids.uuid()
        relay = SessionRelay(session_id, self._linked(), self._mapping)
        tools = await self._tool_servers(servers, relay)
        try:
            opened = await self._assembly.open(
                SessionId(value=session_id), directory, tools, relay, resume=False
            )
        except SessionRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc
        except (SessionOpenError, HistoryError) as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        relay.attach(opened.agent)
        session = AcpSession(session_id, opened, relay, self._mapping)
        self._sessions[session_id] = session
        logger.info(
            "session %s: new, cwd %s, %d MCP server(s)", session_id, cwd, len(servers)
        )

        return schema.NewSessionResponse(
            session_id=session_id,
            config_options=self._options.config_options(self._settings(session)),
        )

    async def _tool_servers(
        self, servers: Sequence[schema.AcpMcpServer], relay: SessionRelay
    ) -> ToolServers | None:
        """Инструменты серверов сессии: каждый отдаёт список до ответа на
        запрос; без серверов — None.

        Ошибки:
        RequestError — сервер не отдал список инструментов (-32603).
        """
        if not servers:
            return None

        side = self._linked().side
        opened: list[ToolServer] = []
        for server in servers:
            tool_server = AcpToolServer(side, server, relay, self._limits)
            try:
                await tool_server.open()
            except SessionOpenError as exc:
                raise RequestError.internal_error(
                    {ErrorDataKey.DETAILS.value: str(exc)}
                ) from exc

            opened.append(tool_server)

        try:
            return ToolServers(opened)
        except ToolServersError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    def _settings(self, session: AcpSession) -> SessionSettings:
        agent = session.agent

        return SessionSettings(
            mode=agent.permission_mode,
            modes=agent.available_modes(),
            model=session.model,
            models=self._assembly.models(),
        )

    async def _announce_settings(self, session: AcpSession) -> None:
        """Полное состояние настроек клиенту: `config_option_update`."""
        settings = self._settings(session)
        await session.relay.notify(self._options.options_update(settings))

    async def prompt(
        self, session_id: str, prompt: list[ContentBlock], **kwargs: Any
    ) -> schema.PromptResponse:
        """Ошибки:
        RequestError — сессия неизвестна (-32002), блоки не годятся (-32602),
            сессию держит другой процесс (-32603).
        """
        held = self._session(session_id)
        asked = self._prompts.render(prompt)
        if isinstance(held, ObserverSession):
            held = await self._claim(session_id, held)

        return await held.prompt(asked)

    async def _claim(self, session_id: str, observer: ObserverSession) -> AcpSession:
        """Наблюдатель берёт замок: сессия открывается по-настоящему и
        занимает его место.

        Ошибки:
        RequestError — сессию держит другой процесс (-32603), записи
            исчезли (-32002), сессию открыть не удалось (-32603).
        """
        try:
            session = await observer.claim()
        except SessionHeldError as exc:
            raise self._held_by_other(session_id, str(exc)) from exc
        except SessionNotFoundError as exc:
            self._sessions.pop(session_id, None)
            await observer.close()
            raise self._unknown(session_id) from exc
        except (SessionOpenError, HistoryError) as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        self._sessions[session_id] = session
        logger.info("session %s: the observer took the lock", session_id)
        await self._announce_settings(session)

        return session

    async def cancel_session(self, session_id: str, **kwargs: Any) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            logger.info("cancel: session %s is unknown; nothing to cancel", session_id)
            return

        if isinstance(session, ObserverSession):
            logger.info("cancel: session %s is observed, no turn to cancel", session_id)
            return

        session.cancel(CoreStopReason.USER_STOP)

    def _session(self, session_id: str) -> HeldSession:
        """Ошибки:
        RequestError — сессия этому процессу неизвестна (-32002).
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise self._unknown(session_id)

        return session

    @staticmethod
    def _unknown(session_id: str) -> RequestError:
        return RequestError(
            AcpErrorCode.RESOURCE_NOT_FOUND,
            f"session {session_id} is unknown to this agent process",
            {ErrorDataKey.SESSION_ID.value: session_id},
        )

    @staticmethod
    def _held_by_other(session_id: str, details: str) -> RequestError:
        return RequestError(
            AcpErrorCode.INTERNAL_ERROR,
            f"session {session_id} is held by another process",
            {
                ErrorDataKey.SESSION_ID.value: session_id,
                ErrorDataKey.DETAILS.value: details,
            },
        )

    @staticmethod
    def _directory(cwd: str) -> Path:
        """Ошибки:
        RequestError — `cwd` не абсолютный (-32602).
        """
        directory = Path(cwd)
        if not directory.is_absolute():
            details = f"cwd must be an absolute path, got '{cwd}'"
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        return directory

    @staticmethod
    def _blocks(mcp_servers: list[McpServerBlock] | None) -> list[McpServerBlock]:
        if mcp_servers is None:
            return []

        return mcp_servers

    @staticmethod
    def _stored_id(session_id: str) -> SessionId:
        """Ошибки:
        RequestError — строка не может быть идентификатором сессии (-32002).
        """
        try:
            return SessionId(value=session_id)
        except ValidationError as exc:
            raise AcpAgent._unknown(session_id) from exc

    def shutdown(self, reason: CoreStopReason) -> None:
        """Прерывает ходы всех сессий: процесс останавливается."""
        for session in self._sessions.values():
            if isinstance(session, AcpSession):
                session.cancel(reason)

    async def close_all(self) -> None:
        for session_id, session in list(self._sessions.items()):
            await session.close()
            self._sessions.pop(session_id, None)

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        replay_from: schema.ReplayFromStartVariant
        | schema.OtherReplayFrom
        | None = None,
        **kwargs: Any,
    ) -> schema.ResumeSessionResponse:
        """Открывает сессию из хранилища; с `replayFrom: start` до ответа
        воспроизводит всю беседу уведомлениями, за ней — заголовок и
        заполненность. Ответ несёт настройки сессии; у наблюдателя агента нет,
        его ответ без них.

        Ошибки:
        RequestError — сессия неизвестна (-32002), `cwd`, серверы или
            `replayFrom` не годятся (-32602), сессию открыть не удалось
            (-32603).
        """
        if isinstance(replay_from, schema.OtherReplayFrom):
            details = (
                f"replayFrom of type '{replay_from.type}' is not supported; "
                "expected type 'start' or no replayFrom"
            )
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        held = await self._open_stored(session_id, cwd, mcp_servers)
        if isinstance(replay_from, schema.ReplayFromStartVariant):
            conversation = held.conversation()
            if isinstance(held, ObserverSession):
                hints: ToolHintSource = held.hints
            else:
                hints = held.agent

            await held.relay.replay(self._mapping.replay(conversation, hints))
            logger.info(
                "session %s: %d record(s) replayed", session_id, len(conversation)
            )

        logger.info("session %s: resumed", session_id)
        if isinstance(held, ObserverSession):
            return schema.ResumeSessionResponse()

        if isinstance(replay_from, schema.ReplayFromStartVariant):
            await held.relay.announce_loaded()

        return schema.ResumeSessionResponse(
            config_options=self._options.config_options(self._settings(held))
        )

    async def _open_stored(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[McpServerBlock] | None,
    ) -> HeldSession:
        """Сессия хранилища: уже открытая в этом процессе — как есть; иначе
        открывается с замком, а при занятом замке — наблюдателем.

        Ошибки:
        RequestError — сессия неизвестна (-32002), параметры не годятся
            (-32602), инструменты или сессию открыть не удалось (-32603).
        """
        directory = self._directory(cwd)
        servers = self._specs.servers(self._blocks(mcp_servers))
        known = self._sessions.get(session_id)
        if known is not None:
            return known

        stored = self._stored_id(session_id)
        relay = SessionRelay(session_id, self._linked(), self._mapping)
        tools = await self._tool_servers(servers, relay)
        try:
            held = await self._held(stored, directory, tools, relay)
        except SessionNotFoundError as exc:
            raise self._unknown(session_id) from exc
        except SessionRefusedError as exc:
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc
        except (SessionOpenError, HistoryError) as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        self._sessions[session_id] = held

        return held

    async def _held(
        self,
        stored: SessionId,
        directory: Path,
        tools: ToolServer | None,
        relay: SessionRelay,
    ) -> HeldSession:
        """Ошибки:
        SessionNotFoundError, SessionRefusedError, SessionOpenError,
        HistoryError — от сборки.
        """
        try:
            opened = await self._assembly.open(
                stored, directory, tools, relay, resume=True
            )
        except SessionHeldError as exc:
            logger.info(
                "session %s: held by another process, opened as an observer (%s)",
                stored.value,
                exc,
            )
            observed = await self._assembly.observe(stored, directory, tools, relay)

            return ObserverSession(stored.value, observed, relay, self._mapping)

        relay.attach(opened.agent)

        return AcpSession(stored.value, opened, relay, self._mapping)

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> schema.ListSessionsResponse:
        """Сессии хранилища страницей: курсор — идентификатор последней
        сессии предыдущей страницы; `cwd` оставляет сессии этого каталога.

        Ошибки:
        RequestError — `cwd` не абсолютный (-32602), хранилище отказало
            (-32603).
        """
        wanted: Path | None = None
        if cwd is not None:
            wanted = self._directory(cwd)

        try:
            page = await self._listed(wanted, cursor)
        except HistoryError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        sessions: list[schema.SessionInfo] = []
        for summary in page.sessions:
            updated_at = datetime.fromisoformat(summary.updated_at)
            sessions.append(
                schema.SessionInfo(
                    session_id=summary.session_id,
                    cwd=summary.cwd,
                    title=self._titles.title(summary.first_prompt),
                    updated_at=updated_at,
                )
            )

        return schema.ListSessionsResponse(
            sessions=sessions, next_cursor=page.next_cursor
        )

    async def _listed(self, wanted: Path | None, cursor: str | None) -> SessionPage:
        """Страница сессий: без каталога — страница хранилища как есть; с
        каталогом страницы хранилища читаются подряд, пока не наберётся
        полная страница сессий этого каталога или сессии не кончатся.
        Курсор ответа — идентификатор последней отданной сессии, так что
        следующая страница продолжает с него.

        Ошибки:
        HistoryError — хранилище отказало.
        """
        page = await self._assembly.sessions(cursor)
        if wanted is None:
            return page

        picked: list[SessionSummary] = []
        while True:
            for summary in page.sessions:
                if Path(summary.cwd) == wanted:
                    picked.append(summary)

            if page.next_cursor is None:
                return SessionPage(sessions=picked, next_cursor=None)

            size = len(page.sessions)
            if len(picked) >= size:
                full = picked[:size]
                return SessionPage(sessions=full, next_cursor=full[-1].session_id)

            page = await self._assembly.sessions(page.next_cursor)

    async def delete_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.DeleteSessionResponse:
        """Сессия, которую ведёт этот процесс, закрывается до удаления (она
        держит замок); наблюдатель закрывается после, когда хранилище
        удаление приняло. Повторное удаление и сессия, которой не было, —
        успех.

        Ошибки:
        RequestError — сессию держит другой процесс или хранилище отказало
            (-32603).
        """
        held = self._sessions.get(session_id)
        if isinstance(held, AcpSession):
            self._sessions.pop(session_id, None)
            await held.close()

        try:
            stored = SessionId(value=session_id)
        except ValidationError:
            logger.info("session %s: cannot exist, nothing to delete", session_id)
            return schema.DeleteSessionResponse()

        await self._delete_stored(session_id, stored)
        if isinstance(held, ObserverSession):
            self._sessions.pop(session_id, None)
            await held.close()

        logger.info("session %s: deleted", session_id)

        return schema.DeleteSessionResponse()

    async def _delete_stored(self, session_id: str, stored: SessionId) -> None:
        """Ошибки:
        RequestError — сессию держит другой процесс или хранилище отказало
            (-32603).
        """
        try:
            await self._assembly.delete(stored)
        except SessionHeldError as exc:
            raise self._held_by_other(session_id, str(exc)) from exc
        except HistoryError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: Any,
        *,
        type: str | None = None,  # noqa: A002 — имя поля протокола
        **kwargs: Any,
    ) -> schema.SetSessionConfigOptionResponse:
        """`mode` — режим разрешений ядра, `model` — профиль модели для
        следующих ходов; ответ — полное состояние настроек, оно же уходит
        `config_option_update`.

        Ошибки:
        RequestError — сессия неизвестна (-32002), настройки или значения
            нет, значение не строка (-32602), ход идёт или профиль не
            годится, сессию держит другой процесс (-32603).
        """
        session = self._settable(session_id)
        option = self._option_id(config_id)
        if not isinstance(value, str):
            details = (
                f"configOption '{config_id}' is a select: value must be a string, "
                f"got {value!r}"
            )
            raise RequestError.invalid_params({ErrorDataKey.DETAILS.value: details})

        if option is ConfigOptionId.MODE:
            await self._set_mode(session, value)
        else:
            name = self._options.parse_model(value, self._settings(session))
            await session.set_model(name)
            logger.info("session %s: model profile switched to %s", session_id, name)

        await self._announce_settings(session)

        return schema.SetSessionConfigOptionResponse(
            config_options=self._options.config_options(self._settings(session))
        )

    async def _set_mode(self, session: AcpSession, value: str) -> None:
        """Ошибки:
        RequestError — режима нет или он недоступен (-32602).
        """
        mode = self._options.parse_mode(value, self._settings(session))
        session.set_mode(mode)
        logger.info(
            "session %s: permission mode switched to %s",
            session.agent.session.id.value,
            mode.value,
        )

    def _settable(self, session_id: str) -> AcpSession:
        """Сессия, настройки которой можно менять: у наблюдателя агента нет.

        Ошибки:
        RequestError — сессия неизвестна (-32002), её держит другой
            процесс (-32603).
        """
        held = self._session(session_id)
        if isinstance(held, ObserverSession):
            details = "its settings can be changed only by the process that holds it"
            raise self._held_by_other(session_id, details)

        return held

    @staticmethod
    def _option_id(config_id: str) -> ConfigOptionId:
        """Ошибки:
        RequestError — такой настройки у агента нет (-32602).
        """
        try:
            return ConfigOptionId(config_id)
        except ValueError as exc:
            known = [option.value for option in ConfigOptionId]
            details = f"configOption '{config_id}' is unknown; available: {known}"
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[McpServerBlock] | None = None,
        **kwargs: Any,
    ) -> schema.ForkSessionResponse:
        """Ветвит сессию хранилища: её записи копируются под новый
        идентификатор (его чеканит агент), и новая сессия открывается как
        обычная — с замком, инструментами серверов запроса и починкой
        оборванного хода загрузчиком. Исходная сессия не меняется, её замок
        не нужен; сессию, которую ведёт этот процесс, перед копией сбрасывает
        в хранилище. Точка среза — расширение `_meta["boba/upToMessageId"]`
        (ForkMeta): ветка от `messageId` вопроса — беседа до этого вопроса;
        срез на первом вопросе даёт пустую сессию. Ответ — `sessionId` новой
        сессии с её настройками, как у `session/new`.

        Ошибки:
        RequestError — сессия неизвестна (-32002); `cwd`, серверы или
            `_meta` не годятся, записи среза в сессии нет (-32602);
            хранилище, инструменты или сборка отказали (-32603).
        """
        directory = self._directory(cwd)
        servers = self._specs.servers(self._blocks(mcp_servers))
        meta = self._fork_meta(kwargs)
        source = self._stored_id(session_id)
        target = SessionId(value=self._ids.uuid())
        relay = SessionRelay(target.value, self._linked(), self._mapping)
        tools = await self._tool_servers(servers, relay)

        await self._flushed(session_id)
        up_to = meta.up_to_message_id
        copied = await self._copied(session_id, source, target, up_to)
        session = await self._opened_fork(target, directory, tools, relay, copied > 0)
        self._sessions[target.value] = session
        logger.info(
            "session %s: forked from %s, %d record(s) copied, cut at %s",
            target.value,
            session_id,
            copied,
            up_to,
        )

        return schema.ForkSessionResponse(
            session_id=target.value,
            config_options=self._options.config_options(self._settings(session)),
        )

    @staticmethod
    def _fork_meta(meta: Mapping[str, Any]) -> ForkMeta:
        """Ошибки:
        RequestError — `boba/upToMessageId` не непустая строка (-32602).
        """
        try:
            return ForkMeta.model_validate(meta)
        except ValidationError as exc:
            details = f"_meta of session/fork is not valid: {exc}"
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

    async def _flushed(self, session_id: str) -> None:
        """Сессия, которую ведёт этот процесс, сбрасывается в хранилище,
        чтобы копия видела все её записи.

        Ошибки:
        RequestError — хранилище отказало (-32603).
        """
        held = self._sessions.get(session_id)
        if not isinstance(held, AcpSession):
            return

        try:
            await held.flush()
        except HistoryError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    async def _copied(
        self, session_id: str, source: SessionId, target: SessionId, up_to: str | None
    ) -> int:
        """Ошибки:
        RequestError — сессия неизвестна (-32002), записи `up_to` нет
            (-32602), хранилище отказало (-32603).
        """
        try:
            return await self._assembly.copy(source, target, up_to)
        except SessionNotFoundError as exc:
            raise self._unknown(session_id) from exc
        except RecordNotFoundError as exc:
            raise RequestError.invalid_params(
                {
                    ErrorDataKey.SESSION_ID.value: session_id,
                    ErrorDataKey.UP_TO_MESSAGE_ID.value: up_to,
                    ErrorDataKey.DETAILS.value: str(exc),
                }
            ) from exc
        except HistoryError as exc:
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

    async def _opened_fork(
        self,
        target: SessionId,
        directory: Path,
        tools: ToolServer | None,
        relay: SessionRelay,
        resume: bool,
    ) -> AcpSession:
        """Новая сессия над скопированными записями; если открыть её не
        удалось, копия удаляется из хранилища, чтобы ветка не осталась
        сиротой.

        Ошибки:
        RequestError — параметры не годятся (-32602), сессию открыть не
            удалось (-32603).
        """
        try:
            opened = await self._assembly.open(
                target, directory, tools, relay, resume=resume
            )
        except SessionRefusedError as exc:
            await self._dropped_fork(target, exc)
            raise RequestError.invalid_params(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc
        except (SessionOpenError, HistoryError) as exc:
            await self._dropped_fork(target, exc)
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: str(exc)}
            ) from exc

        relay.attach(opened.agent)

        return AcpSession(target.value, opened, relay, self._mapping)

    async def _dropped_fork(self, target: SessionId, cause: Exception) -> None:
        """Ошибки:
        RequestError — копию не удалось ни открыть, ни удалить (-32603).
        """
        try:
            await self._assembly.delete(target)
        except HistoryError as exc:
            details = (
                f"the forked session {target.value} could not be opened ({cause}) "
                f"and its copy could not be removed: {exc}"
            )
            raise RequestError.internal_error(
                {ErrorDataKey.DETAILS.value: details}
            ) from exc

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse:
        """Гасит ход сессии и отпускает замок.

        Ошибки:
        RequestError — сессия неизвестна (-32002).
        """
        held = self._session(session_id)
        self._sessions.pop(session_id, None)
        await held.close()
        logger.info("session %s: closed", session_id)

        return schema.CloseSessionResponse()
