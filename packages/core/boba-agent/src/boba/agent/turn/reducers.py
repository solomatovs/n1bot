"""Базовые reducer'ы для TurnSpec — по одному на ось TurnState."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, TypeAlias

from boba.agent.messages import MessageReader
from boba.agent.prompt import PromptFactory, PromptProvider
from boba.agent.turn.spec import TurnState
from boba.llm.models import (
    LLMToolRequest,
    LLMToolSchema,
    SamplingParams,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from boba.patterns import PrioritySource
from boba.schema.declaration import ObjectSchema
from boba.tools.domain import (
    ToolId,
    ToolWireSchemaBuilder,
)
from boba.tools.framework import ToolExecutor

TurnReducer: TypeAlias = PrioritySource[str, TurnState]
"""Alias для reducer'а TurnSpec — стадия сборки TurnState."""


class ModelReducer(PrioritySource[str, TurnState]):
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


class SystemPromptReducer(PrioritySource[str, TurnState]):
    """Собирает system-prompt через PromptFactory каждую итерацию."""

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
        content = PromptFactory(self._providers).build().to_string()
        if content:
            state.system_message = SystemMessage(content=content)

        return state


class HistoryReducer(PrioritySource[str, TurnState]):
    """Копирует весь диалог из MessageReader в state."""

    ID: ClassVar[str] = "history"

    def __init__(self, message_reader: MessageReader, priority: int = 30) -> None:
        self._message_reader = message_reader
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.messages = tuple(self._message_reader.message_iter())
        return state


class ToolsReducer(PrioritySource[str, TurnState]):
    """Каталог tools из ToolExecutor."""

    ID: ClassVar[str] = "tools"

    def __init__(
        self,
        tool_executor: ToolExecutor,
        parallel_tool_calls: bool = True,
        priority: int = 40,
    ) -> None:
        self._tool_executor = tool_executor
        self._parallel = parallel_tool_calls
        self._priority = priority

    def id(self) -> str:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.tools = LLMToolRequest(
            tools=tuple(
                self._tool_to_schema(tid, schema)
                for tid, schema in self._tool_executor.definitions()
            ),
            parallel_tool_calls=self._parallel,
        )
        return state

    @staticmethod
    def _tool_to_schema(
        tool_id: ToolId,
        schema: ObjectSchema[Any],
    ) -> LLMToolSchema:
        """Конверсия (qualified-id, ObjectSchema) в data-only LLMToolSchema."""
        wire = ToolWireSchemaBuilder(schema).build()
        return LLMToolSchema(
            name=tool_id,
            description=schema.description,
            parameters_schema={
                "type": "object",
                "properties": wire.get("properties", {}),
                "required": wire.get("required", []),
            },
        )


class AgentRequestSamplingReducer(PrioritySource[str, TurnState]):
    """Кладёт SamplingParams в state (per-turn инжектится из AgentRequest)."""

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


class RememberUserQueryReducer(PrioritySource[str, TurnState]):
    """После tool-output дублирует последний UserMessage в хвост истории.

    Срабатывает только когда последнее сообщение в state.messages —
    ToolResultMessage, т.е. LLM сейчас будет решать «звать ещё tool
    или отвечать». Цель — удержать фокус LLM на текущей задаче, чтобы
    она не уходила в сторону после длинной серии tool-вызовов.
    Reducer не мутирует сам MessageReader — добавление происходит
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
        if not state.messages:
            return state
        if not isinstance(state.messages[-1], ToolResultMessage):
            return state
        original = self._last_user_content(state.messages)
        if original is None:
            return state
        reminder = UserMessage(content=f"{self._prefix}{original}")
        state.messages = (*state.messages, reminder)
        return state

    @staticmethod
    def _last_user_content(messages: tuple[Any, ...]) -> str | None:
        for m in reversed(messages):
            if isinstance(m, UserMessage):
                return m.content
        return None
