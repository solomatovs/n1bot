"""AgentLoop — чистый оркестратор агентного цикла.

Подготовка контекста через Pipeline (цепочка PipelineStage),
затем цикл LLM → tools → repeat.
"""
from __future__ import annotations

import logging
from typing import Iterator

from openai import OpenAI

from adapters.openai_adapter import to_openai_messages, to_openai_tools
from application.agent.llm_client import LLMStreamConsumer
from application.agent.tool_executor import ToolCallExecutor
from domain.agent.context_filler import ContextRequest
from domain.agent.context_window import ContextWindow
from domain.agent.events import (
    AnswerToken,
    DocPipelineEvent,
    GenerationDone,
    StageCompleted,
)
from domain.core.pipeline import Pipeline

log = logging.getLogger(__name__)

ContextPipeline = Pipeline[ContextRequest, DocPipelineEvent]


class AgentLoop:
    """Агентный цикл: Pipeline fillers → (LLM → tools)* → ответ."""

    def __init__(
        self,
        openai_client: OpenAI,
        context_pipeline: ContextPipeline,
        tool_executor: ToolCallExecutor,
        *,
        max_iterations: int = 10,
    ) -> None:
        self._client = openai_client
        self._context_pipeline = context_pipeline
        self._executor = tool_executor
        self._max_iterations = max_iterations

    def run(self, query: str, model: str) -> Iterator[DocPipelineEvent]:
        """Запустить агентный цикл."""
        # 1. Подготовка context window через Pipeline
        window = ContextWindow()
        yield from self._context_pipeline.run(ContextRequest(window=window, query=query))

        # 2. Цикл: LLM → tools → repeat
        for iteration in range(1, self._max_iterations + 1):
            log.debug("Agent iteration %d/%d", iteration, self._max_iterations)

            # Конвертация domain → OpenAI API (через адаптер, streaming)
            messages = list(to_openai_messages(iter(window.messages)))
            tools = list(to_openai_tools(iter(window.tool_definitions)))

            stream = self._client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                stream=True,
            )

            consumer = LLMStreamConsumer()
            yield from consumer.consume(stream)

            if consumer.has_tool_calls:
                yield from self._executor.execute(consumer.tool_calls, window)
                continue

            yield GenerationDone()
            yield StageCompleted(
                stage="agent_loop",
                detail=f"{len(consumer.answer)} символов, {iteration} итераций",
            )
            return

        yield AnswerToken(token="Достигнут лимит итераций агента. Попробуйте переформулировать вопрос.")
        yield GenerationDone()
        yield StageCompleted(stage="agent_loop", detail=f"лимит {self._max_iterations} итераций")
