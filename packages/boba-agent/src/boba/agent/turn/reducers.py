"""Базовые reducer'ы для TurnSpec — по одному на ось TurnState."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from boba.agent.messages import MessageReader
from boba.agent.prompt import PromptFactory, PromptProvider
from boba.agent.turn.spec import TurnState
from boba.declaration import ObjectSchema
from boba.llm.models import (
    LLMToolRequest,
    LLMToolSchema,
    SamplingParams,
    SystemMessage,
)
from boba.patterns import PrioritySource, StrId
from boba.tools.domain import (
    ToolId,
    ToolWireSchemaBuilder,
)
from boba.tools.framework import ToolsService


class ModelReducer(PrioritySource[StrId, TurnState]):
    """Берёт модель из ctx.agent.agent_request.model."""

    ID: ClassVar[StrId] = StrId("model")

    def __init__(self, model: str, priority: int = 10) -> None:
        self._model = model
        self._priority = priority

    def id(self) -> StrId:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.model = self._model
        return state


class SystemPromptReducer(PrioritySource[StrId, TurnState]):
    """Собирает system-prompt через PromptFactory каждую итерацию."""

    ID: ClassVar[StrId] = StrId("system")

    def __init__(
        self,
        providers: Sequence[PromptProvider],
        priority: int = 20,
    ) -> None:
        self._providers = providers
        self._priority = priority

    def id(self) -> StrId:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        content = PromptFactory(self._providers).build().to_string()
        if content:
            state.system_message = SystemMessage(content=content)

        return state


class HistoryReducer(PrioritySource[StrId, TurnState]):
    """Копирует весь диалог из MessageReader в state."""

    ID: ClassVar[StrId] = StrId("history")

    def __init__(self, message_reader: MessageReader, priority: int = 30) -> None:
        self._message_reader = message_reader
        self._priority = priority

    def id(self) -> StrId:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.messages = tuple(self._message_reader.message_iter())
        return state


class ToolsReducer(PrioritySource[StrId, TurnState]):
    """Каталог tools из ToolsService."""

    ID: ClassVar[StrId] = StrId("tools")

    def __init__(
        self,
        tools_service: ToolsService,
        parallel_tool_calls: bool = True,
        priority: int = 40,
    ) -> None:
        self._tools_service = tools_service
        self._parallel = parallel_tool_calls
        self._priority = priority

    def id(self) -> StrId:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        state.tools = LLMToolRequest(
            tools=tuple(
                self._tool_to_schema(tid, schema)
                for tid, schema in self._tools_service.definitions()
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
            name=tool_id.to_wire(),
            description=schema.description,
            parameters_schema={
                "type": "object",
                "properties": wire.get("properties", {}),
                "required": wire.get("required", []),
            },
        )


class AgentRequestSamplingReducer(PrioritySource[StrId, TurnState]):
    """Кладёт SamplingParams в state (per-turn инжектится из AgentRequest)."""

    ID: ClassVar[StrId] = StrId("sampling")

    def __init__(
        self,
        sampling: SamplingParams | None,
        priority: int = 50,
    ) -> None:
        self._sampling = sampling
        self._priority = priority

    def id(self) -> StrId:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, state: TurnState) -> TurnState:
        if self._sampling is not None:
            state.sampling = self._sampling
        return state
