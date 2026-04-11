"""Стадия: агентный цикл с tool-calling.

LLM сама решает, когда вызывать инструменты и когда дать финальный ответ.
Работает через ToolRegistry — не знает о конкретных инструментах.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterator

from openai import OpenAI

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import (
    AnswerToken,
    DocPipelineEvent,
    GenerationDone,
    ThinkingToken,
    ToolCallStarted,
    ToolResultReady,
)
from application.doc_pipeline.think_parser import ThinkTagParser
from application.doc_pipeline.tools import create_tool_registry
from domain.doc_chat import LLMMessage, LLMRole
from domain.pipeline import StageCompleted, StageStarted
from domain.tools import ToolExecutionError, ToolNotFoundError, ToolRegistry, ToolResult

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pending tool call accumulator (для стримов)
# ---------------------------------------------------------------------------

@dataclass
class _PendingToolCall:
    """Накопитель для стриминга tool_calls по чанкам."""
    id: str = ""
    name: str = ""
    arguments: str = ""


# ---------------------------------------------------------------------------
# AgentLoopStage
# ---------------------------------------------------------------------------

class AgentLoopStage:
    """Агентный цикл: messages + tools → LLM → tool_calls/ответ → повтор."""

    def __init__(
        self,
        openai_client: OpenAI,
        *,
        max_iterations: int = 10,
    ) -> None:
        self._client = openai_client
        self._max_iterations = max_iterations

    @property
    def name(self) -> str:
        return "agent_loop"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)

        registry = create_tool_registry(ctx)
        max_iter = ctx.max_agent_iterations or self._max_iterations

        for iteration in range(1, max_iter + 1):
            log.debug("Agent iteration %d/%d", iteration, max_iter)

            stream = self._client.chat.completions.create(
                model=ctx.model,
                messages=[m.to_dict() for m in ctx.messages],  # type: ignore[arg-type]
                tools=registry.definitions,  # type: ignore[arg-type]
                stream=True,
            )

            pending_calls: dict[int, _PendingToolCall] = {}
            answer_tokens: list[str] = []
            parser = ThinkTagParser()

            for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                # Tool calls (стримятся по частям)
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in pending_calls:
                            pending_calls[idx] = _PendingToolCall()
                        pending = pending_calls[idx]
                        if tc_delta.id:
                            pending.id = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                pending.name = tc_delta.function.name
                            if tc_delta.function.arguments:
                                pending.arguments += tc_delta.function.arguments

                # Thinking tokens (reasoning_content)
                reasoning = getattr(delta, "reasoning_content", None) or ""
                if reasoning:
                    yield ThinkingToken(token=reasoning)

                # Content tokens
                content = getattr(delta, "content", None) or ""
                if content:
                    for fragment in parser.feed(content):
                        if not fragment.text:
                            continue
                        if fragment.role.value == "thinking":
                            yield ThinkingToken(token=fragment.text)
                        else:
                            answer_tokens.append(fragment.text)
                            yield AnswerToken(token=fragment.text)

            if pending_calls:
                yield from self._handle_tool_calls(ctx, registry, pending_calls)
                continue

            # Текстовый ответ — финал
            answer = "".join(answer_tokens)
            yield GenerationDone()
            yield StageCompleted(
                stage=self.name,
                detail=f"{len(answer)} символов, {iteration} итераций",
            )
            return

        # Лимит итераций
        yield AnswerToken(token="Достигнут лимит итераций агента. Попробуйте переформулировать вопрос.")
        yield GenerationDone()
        yield StageCompleted(stage=self.name, detail=f"лимит {max_iter} итераций")

    # -----------------------------------------------------------------------
    # Обработка tool_calls
    # -----------------------------------------------------------------------

    def _handle_tool_calls(
        self,
        ctx: DocPipelineContext,
        registry: ToolRegistry,
        pending_calls: dict[int, _PendingToolCall],
    ) -> Iterator[DocPipelineEvent]:
        """Выполнить все tool calls из одной итерации."""

        openai_tool_calls: list[dict] = []
        for idx in sorted(pending_calls):
            tc = pending_calls[idx]
            openai_tool_calls.append({
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            })

        ctx.messages.append(LLMMessage(
            role=LLMRole.ASSISTANT,
            tool_calls=openai_tool_calls,
        ))

        for tc_dict in openai_tool_calls:
            tc_id = tc_dict["id"]
            tc_name = tc_dict["function"]["name"]
            tc_args_str = tc_dict["function"]["arguments"]

            yield ToolCallStarted(
                tool_call_id=tc_id,
                tool_name=tc_name,
                arguments=tc_args_str,
            )

            try:
                arguments = json.loads(tc_args_str) if tc_args_str else {}
            except json.JSONDecodeError:
                arguments = {}

            try:
                result_text = ""
                for event in registry.execute(tc_name, **arguments):
                    if isinstance(event, ToolResult):
                        result_text = event.content
                    else:
                        yield event  # pipeline-события для UI — сразу
            except (ToolNotFoundError, ToolExecutionError) as exc:
                result_text = str(exc)

            yield ToolResultReady(
                tool_call_id=tc_id,
                tool_name=tc_name,
                content=result_text[:2000],
            )

            ctx.messages.append(LLMMessage(
                role=LLMRole.TOOL,
                content=result_text,
                tool_call_id=tc_id,
            ))
