"""Базовые reducer'ы для TurnSpec — по одному на ось TurnState."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from boba.domain.agent.prompt import PromptFactory, PromptProvider
from boba.domain.agent.turn.spec import TurnResolveContext, TurnState
from boba.domain.core.tools import Tool, ToolsService
from boba.domain.llm.models import (
    LLMMessage,
    LLMToolRequest,
    LLMToolSchema,
)
from boba.patterns import ContextPrioritySource, StrId

_MODEL_ID = StrId("model")
_SYSTEM_ID = StrId("system")
_HISTORY_ID = StrId("history")
_TOOLS_ID = StrId("tools")
_SAMPLING_ID = StrId("sampling")


def _tool_to_schema(tool: Tool[Any]) -> LLMToolSchema:
    """Конверсия доменного Tool в data-only LLMToolSchema."""
    schema = tool.definition()
    wire = schema.build_wire_schema()
    return LLMToolSchema(
        name=tool.tool_id().to_wire(),
        description=schema.description,
        parameters_schema={
            "type": "object",
            "properties": wire.properties,
            "required": wire.required,
        },
    )


class ModelReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Берёт модель из ctx.agent.agent_request.model."""

    def __init__(self, priority: int = 10) -> None:
        self._priority = priority

    def id(self) -> StrId:
        return _MODEL_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        state.model = ctx.agent.agent_request.model
        return state


class SystemPromptReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Собирает system-prompt через PromptFactory каждую итерацию."""

    def __init__(
        self,
        providers: Sequence[PromptProvider],
        priority: int = 20,
    ) -> None:
        self._providers = providers
        self._priority = priority

    def id(self) -> StrId:
        return _SYSTEM_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        content = (
            PromptFactory(ctx.agent, self._providers)
            .build()
            .to_string()
        )
        if content:
            state.system_message = LLMMessage(role="system", content=content)
        return state


class HistoryReducer(ContextPrioritySource[TurnResolveContext, StrId, TurnState]):
    """Копирует весь диалог из MessageReader в state."""

    def __init__(self, priority: int = 30) -> None:
        self._priority = priority

    def id(self) -> StrId:
        return _HISTORY_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        state.messages = tuple(ctx.message_reader.message_iter())
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
        return _HISTORY_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        history = list(ctx.message_reader.message_iter())
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
        return _TOOLS_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        state.tools = LLMToolRequest(
            tools=tuple(_tool_to_schema(t) for t in self._tools_service.tools()),
            parallel_tool_calls=self._parallel,
        )
        return state


class AgentRequestSamplingReducer(
    ContextPrioritySource[TurnResolveContext, StrId, TurnState]
):
    """Берёт SamplingParams из ctx.agent.agent_request.sampling."""

    def __init__(self, priority: int = 50) -> None:
        self._priority = priority

    def id(self) -> StrId:
        return _SAMPLING_ID

    def priority(self) -> int:
        return self._priority

    def apply(self, ctx: TurnResolveContext, state: TurnState) -> TurnState:
        sampling = ctx.agent.agent_request.sampling
        if sampling is not None:
            state.sampling = sampling
        return state
