"""Стадии AgentLoop — каждая является StreamSource[AgentContext, AgentEvent]."""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from boba.domain.agent.events import (
    AgentEvent,
    AnswerToken,
    GenerationDone,
    StageCompleted,
    StageStarted,
    ThinkingToken,
    ToolCallStarted,
    ToolResultReady,
)
from boba.domain.agent.llm import LLMClient, LLMMessage, LLMRequest, LLMToolCall
from boba.domain.agent.models import AgentContext
from boba.domain.core.promt import SystemPromptService
from boba.domain.core.stream import StreamSource
from boba.domain.core.tools import ToolDefinition, ToolId, ToolsService

logger = logging.getLogger(__name__)


class BuildMessagesStage(StreamSource[AgentContext, AgentEvent]):
    """
    Первая стадия: на самой первой итерации формирует system + user message.
    На последующих — ничего не делает (messages уже содержат историю).
    """

    def __init__(self, prompt_service: SystemPromptService) -> None:
        self._prompt_service = prompt_service

    def name(self) -> str:
        return "BuildMessages"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        if ctx.messages:
            return

        yield StageStarted(stage=self.name())

        system_prompt = self._prompt_service.build().build()
        ctx.messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=ctx.request.query),
        ]

        yield StageCompleted(stage=self.name(), detail="messages initialized")


class GenerateStage(StreamSource[AgentContext, AgentEvent]):
    """
    Вызывает LLM (стриминг). Стримит ThinkingToken/AnswerToken события.
    По завершении добавляет assistant message в ctx.messages
    и складывает tool_calls в ctx.pending_tool_calls.
    """

    def __init__(self, llm: LLMClient, tools_service: ToolsService) -> None:
        self._llm = llm
        self._tools_service = tools_service

    def name(self) -> str:
        return "Generate"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ctx.iteration += 1
        yield StageStarted(stage=self.name())

        tool_defs = self._build_tool_defs()
        request = LLMRequest(
            model=ctx.request.model or ctx.config.default_model,
            messages=ctx.messages,
            tools=tool_defs or None,
            max_tokens=ctx.request.max_tokens,
        )

        content_parts: list[str] = []
        all_tool_calls: list[LLMToolCall] = []

        for delta in self._llm.stream(request):
            if delta.thinking:
                yield ThinkingToken(token=delta.thinking)
            if delta.content:
                content_parts.append(delta.content)
                yield AnswerToken(token=delta.content)
            if delta.tool_calls:
                all_tool_calls.extend(delta.tool_calls)

        # Собираем assistant message
        content = "".join(content_parts)
        ctx.messages.append(
            LLMMessage(role="assistant", content=content, tool_calls=all_tool_calls),
        )
        ctx.pending_tool_calls = list(all_tool_calls)

        yield GenerationDone()
        yield StageCompleted(
            stage=self.name(),
            detail=f"tokens={len(content)}, tool_calls={len(all_tool_calls)}",
        )

    def _build_tool_defs(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for defn in self._tools_service.get_definitions():
            result.append(_tool_def_to_dict(defn))
        return result


class ToolExecutionStage(StreamSource[AgentContext, AgentEvent]):
    """
    Выполняет pending tool_calls из контекста.
    Добавляет tool-result messages обратно в ctx.messages.
    """

    def __init__(self, tools_service: ToolsService) -> None:
        self._tools_service = tools_service

    def name(self) -> str:
        return "ToolExecution"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        if not ctx.pending_tool_calls:
            return

        yield StageStarted(stage=self.name())

        for tc in ctx.pending_tool_calls:
            yield ToolCallStarted(
                tool_call_id=tc.id,
                tool_name=tc.name,
                arguments=tc.arguments,
            )

            raw_args = json.loads(tc.arguments) if tc.arguments else {}
            result = self._tools_service.execute(ToolId(tc.name), raw_args)

            ctx.messages.append(
                LLMMessage(
                    role="tool",
                    content=result.content,
                    tool_call_id=tc.id,
                ),
            )

            yield ToolResultReady(
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=result.content,
                is_error=result.is_error,
            )

        ctx.pending_tool_calls.clear()
        yield StageCompleted(
            stage=self.name(),
            detail=f"executed {len(ctx.pending_tool_calls)} tools",
        )


# ── helpers ──


def _tool_def_to_dict(defn: ToolDefinition) -> dict[str, Any]:
    """Конвертирует ToolDefinition в OpenAI-совместимый формат."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for p in defn.input_schema.params:
        properties[p.name] = {
            "type": p.type.value,
            "description": p.description,
        }
        if p.required:
            required.append(p.name)

    return {
        "type": "function",
        "function": {
            "name": defn.id.name,
            "description": defn.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }
