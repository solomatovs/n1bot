"""Langchain-tracer: процесс ответа одним сворачиваемым шагом.

Лента: вопрос и итоговый ответ на верхнем уровне (их рисует callback),
внутри контейнера «process...» — thinking и шаг на каждый инструмент.
Экземпляр живёт один on_message, поэтому контейнер всегда один.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

from chainlit.context import context_var
from chainlit.langchain.callbacks import process_content
from chainlit.message import Message
from chainlit.step import Step
from chainlit.utils import utc_now
from langchain_core.outputs import ChatGenerationChunk, GenerationChunk
from langchain_core.tracers.base import AsyncBaseTracer
from literalai.observability.step import TrueStepType
from typing_extensions import override

from boba.chainlit2.rendering.chart_figure import build_plotly_element
from boba.chainlit2.rendering.result_view import (
    ChartRendering,
    MarkdownRendering,
    ToolResultView,
)
from boba.chainlit2.rendering.tool_result import (
    ErrorResult,
    ToolResult,
    ToolResultBase,
)

__all__ = ["AgentTracer"]

logger = logging.getLogger(__name__)

_CONTAINER_TYPE = "run"


class AgentTracer(AsyncBaseTracer):
    """Трасит один агентский цикл и рисует step-иерархию процесса ответа."""

    def __init__(self) -> None:
        super().__init__()
        self._context = context_var.get()
        self._container: Step | None = None
        # run_id вызова llm -> накопленный reasoning-контент
        self._reasoning: dict[str, str] = {}
        # run_id -> Step инструмента (выполняется -> результат)
        self._tool_steps: dict[str, Step] = {}

    def _set_context(self) -> None:
        context_var.set(self._context)

    async def _container_step(self) -> Step:
        """Шаг-«контейнер» процесса ответа (создаётся один раз)."""
        if self._container is not None:
            return self._container
        step = Step(
            name="process...",
            type=_CONTAINER_TYPE,
            parent_id=None,
            default_open=False,
            auto_collapse=True,
        )
        await step.send()
        self._container = step
        return step

    async def _event_step(
        self,
        *,
        name: str,
        step_type: TrueStepType,
        input_text: str | None = None,
        output_text: str | None = None,
    ) -> Step:
        """Дочерний step контейнера: одно событие процесса (llm/tool)."""
        container = await self._container_step()
        step = Step(
            name=name,
            type=step_type,
            parent_id=container.id,
            default_open=False,
            auto_collapse=True,
        )
        if input_text:
            step.input = input_text
        if output_text:
            step.output = output_text
        step.start = utc_now()
        step.end = utc_now()
        await step.send()
        return step

    async def _finalize_tool_result(self, step: Step, artifact: Any) -> None:
        """Отрендерить ToolResult в step'е tool result (markdown/chart/error)."""
        if not isinstance(artifact, ToolResultBase):
            content, lang = process_content(artifact)
            step.output = content
            step.language = lang
            return
        result = cast(ToolResult, artifact)
        match ToolResultView(result).render():
            case ChartRendering(spec=spec, title=title):
                step.output = (
                    f"график отрисован: {title}" if title else "график отрисован"
                )
                await step.update()
                await self._send_chart_message(title, spec)
            case MarkdownRendering(markdown=markdown):
                # language не ставим: с ним chainlit рендерит код-блок
                step.output = markdown
                step.is_error = isinstance(result, ErrorResult)
                await step.update()

    async def _send_chart_message(
        self,
        title: str | None,
        spec: Mapping[str, Any],
    ) -> None:
        """Топ-левел cl.Message с inline-графиком (вне сворачиваемого step'а)."""
        try:
            element = build_plotly_element(title or "chart", dict(spec))
            message = Message(content=title or "", elements=[element])
            # без обнуления parent график всплывёт над контейнером
            message.parent_id = None
            await message.send()
        except Exception:
            logger.exception("не удалось отрисовать график")

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
        run_key = str(run_id)
        # reasoning: атрибут или additional_kwargs (ReasoningChatOpenAI)
        msg = getattr(chunk, "message", None)
        if msg is not None:
            reasoning = getattr(msg, "reasoning_content", None) or (
                getattr(msg, "additional_kwargs", None) or {}
            ).get("reasoning_content")
            if reasoning:
                self._reasoning[run_key] = (
                    self._reasoning.get(run_key, "") + str(reasoning)
                )
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
        run_key = str(run_id)
        reasoning = self._reasoning.pop(run_key, "")

        message: Any = None
        if response.generations and response.generations[0]:
            message = getattr(response.generations[0][0], "message", None)

        # из llm-события рисуем только thinking: ответ пользователь
        # видит итоговым сообщением, вызовы инструментов — tool-шагами
        text = (
            reasoning
            or getattr(message, "reasoning_content", None)
            or (getattr(message, "additional_kwargs", None) or {}).get(
                "reasoning_content"
            )
        )
        if text:
            await self._event_step(
                name="thinking",
                step_type="llm",
                output_text=str(text),
            )
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
        container = await self._container_step()
        step = Step(
            name=tool_name,
            type="tool",
            parent_id=container.id,
            default_open=False,
            auto_collapse=True,
        )
        if inputs:
            step.input = _render_args(inputs)
        step.output = "выполняется"
        step.start = utc_now()
        await step.send()
        self._tool_steps[str(run_id)] = step
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
        step = self._tool_steps.pop(str(run_id), None)
        if step is not None:
            artifact = getattr(output, "artifact", None)
            if artifact is not None:
                await self._finalize_tool_result(step, artifact)
            else:
                content, lang = process_content(output)
                step.output = content
                step.language = lang
            step.end = utc_now()
            await step.update()
        return await super().on_tool_end(output, run_id=run_id, **kwargs)

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
        step = self._tool_steps.pop(str(run_id), None)
        if step is not None:
            step.is_error = True
            step.output = f"**tool failed:** {error}"
            step.end = utc_now()
            await step.update()
        return await super().on_tool_error(
            error, run_id=run_id, parent_run_id=parent_run_id, tags=tags, **kwargs,
        )

    @override
    async def _persist_run(self, run: Any) -> None:
        """Историю пишет chainlit data layer, трасеру персистить нечего."""


def _render_args(args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    return json.dumps(args, ensure_ascii=False, indent=2)
