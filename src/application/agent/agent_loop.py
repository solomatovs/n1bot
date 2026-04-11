"""AgentLoop — главный компонент агента.

Создаётся один раз через bootstrap. Содержит все зависимости.
При каждом вызове run() сам собирает workspace, registry, context window.

UI:
    agent = services.create_agent(folder_path, history_path)
    for event in agent.run(query, model):
        handle(event)
"""
from __future__ import annotations

import logging
from typing import Iterator, Sequence

from openai import OpenAI

from application.agent.llm_client import LLMStreamConsumer
from application.agent.system_prompt import build_system_prompt
from application.agent.tool_executor import ToolCallExecutor
from domain.agent.context_window import ContextWindow
from domain.agent.events import (
    AnswerToken,
    DocPipelineEvent,
    GenerationDone,
    StageCompleted,
    StageStarted,
)
from domain.agent.tools import Tool, ToolRegistry
from domain.workspace import Workspace

log = logging.getLogger(__name__)


class AgentLoop:
    """Агент — самодостаточный компонент.

    Содержит workspace, registry, openai_client.
    run(query, model) — единственный публичный метод.
    """

    def __init__(
        self,
        openai_client: OpenAI,
        workspace: Workspace,
        tools: Sequence[Tool],
        *,
        max_iterations: int = 10,
    ) -> None:
        self._client = openai_client
        self._workspace = workspace
        self._registry: ToolRegistry = ToolRegistry(tools)
        self._system_prompt = build_system_prompt(self._registry)
        self._max_iterations = max_iterations

    def run(self, query: str, model: str) -> Iterator[DocPipelineEvent]:
        """Запустить агентный цикл для запроса.

        Сам создаёт context window, наполняет system prompt + query,
        крутит цикл LLM → tools → repeat.
        """
        yield StageStarted(stage="agent_loop")

        window = ContextWindow()
        window.add_system(self._system_prompt)
        window.add_user(query)

        executor = ToolCallExecutor(self._registry)

        for iteration in range(1, self._max_iterations + 1):
            log.debug("Agent iteration %d/%d", iteration, self._max_iterations)

            stream = self._client.chat.completions.create(
                model=model,
                messages=window.to_messages(),  # type: ignore[arg-type]
                tools=self._registry.definitions,  # type: ignore[arg-type]
                stream=True,
            )

            consumer = LLMStreamConsumer()
            yield from consumer.consume(stream)

            if consumer.has_tool_calls:
                yield from executor.execute(consumer.tool_calls, window)
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
