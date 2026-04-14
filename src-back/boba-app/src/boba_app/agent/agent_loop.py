"""AgentLoop — чистый оркестратор агентного цикла.

Подготовка контекста через Pipeline (цепочка PipelineStage),
затем цикл LLM → tools → repeat.
"""

from __future__ import annotations

import logging
from typing import Iterator

from boba_app.agent.completion_middleware import CompletionMiddleware
from boba_app.agent.llm_client import LLMStreamConsumer
from boba_app.agent.tool_executor import ToolCallExecutor
from boba_domain.agent.config import AgentConfig, AgentRequest
from boba_domain.agent.context_filler import ContextRequest
from boba_domain.agent.context_window import ContextWindow
from boba_domain.agent.events import (
    AnswerToken,
    DocPipelineEvent,
    GenerationDone,
    StageCompleted,
)
from boba_domain.core.llm_service import LLMCompletionService
from boba_domain.core.pipeline import Pipeline

log = logging.getLogger(__name__)

ContextPipeline = Pipeline[ContextRequest, DocPipelineEvent]


class AgentLoop:
    """Агентный цикл: Pipeline fillers → (LLM → tools)* → ответ."""

    def __init__(
        self,
        config: AgentConfig,
        llm: LLMCompletionService,
        middleware: CompletionMiddleware,
        context_pipeline: ContextPipeline,
        tool_executor: ToolCallExecutor,
    ) -> None:
        self._config = config
        self._llm = llm
        self._middleware = middleware
        self._context_pipeline = context_pipeline
        self._executor = tool_executor

    def run(self, request: AgentRequest) -> Iterator[DocPipelineEvent]:
        """Запустить агентный цикл."""
        window = ContextWindow()
        yield from self._context_pipeline.run(
            ContextRequest(window=window, query=request.query)
        )

        for iteration in range(1, self._config.max_iterations + 1):
            log.debug("Agent iteration %d/%d", iteration, self._config.max_iterations)

            raw_deltas = self._llm.stream_completion(
                messages=window.messages,
                tool_definitions=window.tool_definitions,
                model=request.model,
            )
            deltas = self._middleware.process(raw_deltas)

            consumer = LLMStreamConsumer()
            yield from consumer.consume(deltas)

            if consumer.has_tool_calls:
                yield from self._executor.execute(consumer.tool_calls, window)
                continue

            yield GenerationDone()
            yield StageCompleted(
                stage="agent_loop",
                detail=f"{len(consumer.answer)} символов, {iteration} итераций",
            )
            return

        yield AnswerToken(token=self._config.limit_message)
        yield GenerationDone()
        yield StageCompleted(
            stage="agent_loop", detail=f"лимит {self._config.max_iterations} итераций"
        )
