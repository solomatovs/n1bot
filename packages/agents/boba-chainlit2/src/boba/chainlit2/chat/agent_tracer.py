"""Langchain-tracer: процесс ответа одним сворачиваемым шагом.

Раскладку и отрисовку результатов держит ChatView — тот же, которым
восстанавливается история треда. Экземпляр живёт один on_message,
поэтому контейнер процесса всегда один.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from chainlit.context import context_var
from chainlit.step import Step
from langchain_core.outputs import ChatGenerationChunk, GenerationChunk
from langchain_core.tracers.base import AsyncBaseTracer
from typing_extensions import override

from boba.chainlit2.rendering.chat_view import ChatView

__all__ = ["AgentTracer"]

logger = logging.getLogger(__name__)


class AgentTracer(AsyncBaseTracer):
    """Трасит один агентский цикл и рисует step-иерархию процесса ответа."""

    def __init__(self, view: ChatView) -> None:
        super().__init__()
        self._context = context_var.get()
        self._view = view
        self._reasoning: dict[str, str] = {}
        self._tool_steps: dict[str, Step] = {}

    @property
    def view(self) -> ChatView:
        """Лента, в которую трасер пишет шаги."""
        return self._view

    def _set_context(self) -> None:
        context_var.set(self._context)

    @staticmethod
    def _reasoning_of(message: Any) -> str:
        if message is None:
            return ""
        value = getattr(message, "reasoning_content", None) or (
            getattr(message, "additional_kwargs", None) or {}
        ).get("reasoning_content")
        return str(value) if value else ""

    @override
    async def on_llm_new_token(
        self,
        token: str,
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        if reasoning := self._reasoning_of(getattr(chunk, "message", None)):
            run_key = str(run_id)
            if run_key not in self._reasoning:
                await self._view.container(run_key)
            self._reasoning[run_key] = self._reasoning.get(run_key, "") + reasoning
        return await super().on_llm_new_token(
            token, chunk=chunk, run_id=run_id, parent_run_id=parent_run_id, **kwargs,
        )

    @override
    async def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        reasoning = self._reasoning.pop(str(run_id), "")

        message: Any = None
        if response.generations and response.generations[0]:
            message = getattr(response.generations[0][0], "message", None)

        if text := (reasoning or self._reasoning_of(message)):
            await self._view.thinking(text)

        return await super().on_llm_end(
            response, run_id=run_id, parent_run_id=parent_run_id, **kwargs,
        )

    @override
    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        tags: list[str] | None = None,
        parent_run_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        name: str | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        tool_name = name or (serialized or {}).get("name", "tool")
        self._tool_steps[str(run_id)] = await self._view.tool_started(tool_name, inputs)
        return await super().on_tool_start(
            serialized, input_str, run_id=run_id, parent_run_id=parent_run_id,
            tags=tags, metadata=metadata, name=name, inputs=inputs, **kwargs,
        )

    @override
    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        if step := self._tool_steps.pop(str(run_id), None):
            if getattr(output, "status", None) == "error":
                await self._view.tool_failed(step, getattr(output, "content", output))
            else:
                artifact = getattr(output, "artifact", None)
                await self._view.tool_finished(
                    step,
                    artifact if artifact is not None else output,
                    getattr(output, "tool_call_id", None),
                )
        return await super().on_tool_end(output, run_id=run_id, **kwargs)

    async def stop_pending(self) -> None:
        """Закрыть шаги инструментов, оставшиеся в работе после остановки."""
        self._set_context()
        while self._tool_steps:
            _, step = self._tool_steps.popitem()
            await self._view.tool_stopped(step)

    @override
    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        if step := self._tool_steps.pop(str(run_id), None):
            await self._view.tool_failed(step, error)
        return await super().on_tool_error(
            error, run_id=run_id, parent_run_id=parent_run_id, tags=tags, **kwargs,
        )

    @override
    async def _persist_run(self, run: Any) -> None:
        pass
