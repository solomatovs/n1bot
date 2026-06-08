"""Базовые reducer'ы для TurnSpec — по одному на ось TurnState."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, TypeAlias

from boba.agent.prompt import PromptFactory, PromptProvider
from boba.agent.turn.history_view import HistoryDialogView
from boba.agent.turn.spec import TurnState
from boba.llm.models import (
    LLMToolDefinition,
    RequestId,
    SamplingParams,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from boba.patterns import PrioritySource
from boba.tools.framework import ToolCatalog

TurnReducer: TypeAlias = PrioritySource[str, TurnState]
"""Alias для reducer'а TurnSpec — стадия сборки TurnState."""


class RequestIdFromReducer(TurnReducer):
    """Кладёт request_id в TurnState — берётся из ctx.request_id."""

    ID: ClassVar[str] = "request_id"

    def __init__(self, request_id: RequestId, priority: int = 5) -> None:
        self._request_id = request_id
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.request_id = self._request_id
        return state


class ModelFromRequestReducer(TurnReducer):
    """Берёт модель из ctx.agent.agent_request.model."""

    ID: ClassVar[str] = "model"

    def __init__(self, model: str, priority: int = 10) -> None:
        self._model = model
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.model = self._model
        return state


class StreamModeReducer(TurnReducer):
    """Флаг stream в state: True=поток дельт, False=один итоговый ответ."""

    ID: ClassVar[str] = "stream"

    def __init__(self, stream: bool, priority: int = 15) -> None:
        self._stream = stream
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.stream = self._stream
        return state


class SystemPromptReducer(TurnReducer):
    """Собирает system-сообщения через PromptFactory каждую итерацию."""

    ID: ClassVar[str] = "system"

    def __init__(
        self,
        providers: Sequence[PromptProvider],
        priority: int = 20,
    ) -> None:
        self._providers = providers
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        result = PromptFactory(self._providers).build()

        state.system_messages = tuple(
            SystemMessage.from_text(block.content) for block in result.blocks()
        )

        return state


class UserQueryReducer(TurnReducer):
    """
    Добавляет UserMessage.from_text(ctx.query) к dialog_messages
    """

    ID: ClassVar[str] = "user_query"

    def __init__(self, query: str, priority: int = 32) -> None:
        self._query = query
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.dialog_messages = (
            UserMessage.from_text(self._query),
            *state.dialog_messages,
        )
        return state


class HistoryReducer(TurnReducer):
    """Копирует диалог из HistoryDialogView в state.dialog_messages.

    Источник — журнал HistoryService, отфильтрованный view'хой до тех
    AgentEvent, которые относятся к диалогу пользователь ↔ чатбот:
    UserMessage, AssistantMessage (склеенный из снапшотов одной
    генерации) и ToolResultMessage. SystemMessage не сохраняется в
    истории — system-блоки собираются каждый turn SystemPromptReducer.
    """

    ID: ClassVar[str] = "history"

    def __init__(self, history_view: HistoryDialogView, priority: int = 30) -> None:
        self._history_view = history_view
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.dialog_messages = tuple(self._history_view.dialog_message_iter())
        return state


class ToolsDefinitionReducer(TurnReducer):
    """Каталог tools из ToolCatalog."""

    ID: ClassVar[str] = "tools"

    def __init__(
        self,
        catalog: ToolCatalog,
        parallel_tool_calls: bool = True,
        priority: int = 40,
    ) -> None:
        self._catalog = catalog
        self._parallel = parallel_tool_calls
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.tools = LLMToolDefinition(
            tools=tuple(self._catalog.definitions()),
            parallel_tool_calls=self._parallel,
        )
        return state


class SamplingReducer(TurnReducer):
    """Кладёт SamplingParams в state. Конфигурируется один раз на этапе сборки."""

    ID: ClassVar[str] = "sampling"

    def __init__(
        self,
        sampling: SamplingParams | None,
        priority: int = 50,
    ) -> None:
        self._sampling = sampling
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        if self._sampling is not None:
            state.sampling = self._sampling
        return state


class RememberUserQueryReducer(TurnReducer):
    """После tool-output дублирует последний UserMessage в хвост истории.

    Срабатывает только когда последнее сообщение в state.messages —
    ToolResultMessage, т.е. LLM сейчас будет решать «звать ещё tool
    или отвечать». Цель — удержать фокус LLM на текущей задаче, чтобы
    она не уходила в сторону после длинной серии tool-вызовов.
    Reducer не мутирует сам журнал — добавление происходит
    в state каждую итерацию, без persistence.
    """

    ID: ClassVar[str] = "remember_user_query"
    DEFAULT_PREFIX: ClassVar[str] = "Напоминание об исходном запросе: "

    def __init__(
        self,
        prefix: str = DEFAULT_PREFIX,
        priority: int = 35,
    ) -> None:
        self._prefix = prefix
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        if not state.dialog_messages:
            return state
        if not isinstance(state.dialog_messages[-1], ToolResultMessage):
            return state
        original = self._last_user_content(state.dialog_messages)
        if original is None:
            return state
        reminder = UserMessage.from_text(f"{self._prefix}{original}")
        state.dialog_messages = (*state.dialog_messages, reminder)
        return state

    @staticmethod
    def _last_user_content(messages: tuple[Any, ...]) -> str | None:
        for m in reversed(messages):
            if isinstance(m, UserMessage):
                return m.content
        return None
