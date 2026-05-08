"""Базовые reducer'ы для TurnSpec — по одному на ось TurnState."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from boba.agent.messages import MessageService
from boba.agent.prompt import PromptFactory, PromptProvider
from boba.agent.turn.spec import TurnResolveContext, TurnState
from boba.declaration import ObjectSchema
from boba.llm.models import (
    LLMMessage,
    LLMToolRequest,
    LLMToolSchema,
)
from boba.patterns import ContextPrioritySource, StrId
from boba.tools.domain import (
    ToolId,
    ToolWireSchemaBuilder,
)
from boba.tools.framework import ToolsService


class ModelReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Берёт модель из ctx.agent.agent_request.model."""

    ID: ClassVar[StrId] = StrId("model")

    def __init__(self, priority: int = 10) -> None:
        self._priority = priority

    def id(self) -> StrId:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        state.model = ctx.agent.request.model
        return state


class SystemPromptReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
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

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        content = PromptFactory(ctx.agent, self._providers).build().to_string()
        if content:
            state.system_message = LLMMessage(role="system", content=content)
        return state


class HistoryReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Копирует весь диалог из MessageReader в state."""

    ID: ClassVar[StrId] = StrId("history")

    def __init__(self, priority: int = 30) -> None:
        self._priority = priority

    def id(self) -> StrId:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        messages = ctx.channels.get(MessageService.channel_id())
        state.messages = tuple(messages.message_iter())
        return state


class HistoryWithTaskAnchorReducer(
    ContextPrioritySource[TurnResolveContext, StrId, TurnState],
):
    """История + ephemeral-reminder исходной задачи после tool_result."""

    def __init__(
        self,
        priority: int = 30,
        min_tool_content_chars: int = 0,
    ) -> None:
        self._priority = priority
        self._min_tool_content_chars = min_tool_content_chars

    def id(self) -> StrId:
        return HistoryReducer.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        messages = ctx.channels.get(MessageService.channel_id())
        history = list(messages.message_iter())
        anchor = self._maybe_anchor(history)
        if anchor is not None:
            history.append(anchor)
        state.messages = tuple(history)
        return state

    def _maybe_anchor(self, history: list[LLMMessage]) -> LLMMessage | None:
        if not history:
            return None
        last = history[-1]
        if last.role != "tool":
            return None
        if len(last.content) < self._min_tool_content_chars:
            return None
        original = self._last_user_query(history)
        if original is None:
            return None
        return LLMMessage(
            role="system",
            content=(
                f'Reminder: исходная задача пользователя — "{original}". '
                f"Продолжай работу над ней, опираясь на результат "
                f"последнего tool_call."
            ),
        )

    @staticmethod
    def _last_user_query(history: list[LLMMessage]) -> str | None:
        for msg in reversed(history):
            if msg.role == "user":
                return msg.content
        return None


class ToolsReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
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

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
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


class AgentRequestSamplingReducer(
    ContextPrioritySource[TurnResolveContext, StrId, TurnState]
):
    """Берёт SamplingParams из ctx.agent.agent_request.sampling."""

    ID: ClassVar[StrId] = StrId("sampling")

    def __init__(self, priority: int = 50) -> None:
        self._priority = priority

    def id(self) -> StrId:
        return self.ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        sampling = ctx.agent.request.sampling
        if sampling is not None:
            state.sampling = sampling
        return state
