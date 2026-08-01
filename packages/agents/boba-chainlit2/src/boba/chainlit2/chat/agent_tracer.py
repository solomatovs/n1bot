"""Собственный langchain-tracer: рисует процесс ответа одним сворачиваемым шагом.

Пишется с нуля (не из community-трасера chainlit): наследуется от
AsyncBaseTracer только как механизм подписки на события langchain, без
literalai-генераций, GenerationHelper и FinalStreamHelper.

Целевая структура ленты (chainlit steps):
- на верхнем уровне — сообщение пользователя и итоговый ответ модели
  (их рисует callback, не этот tracer);
- один шаг-«контейнер» процесса ответа (parent=вопрос), внутри него по
  одному дочернему шагу на каждое событие:
      thinking     — reasoning-токены (если модель их отдаёт);
      <имя tool>   — один шаг на инструмент: «выполняется», пока tool
                     работает, затем — результат (рендер ToolResult).
  Сам ответ llm в процессе не показываем: вызов инструментов виден по
  tool-шагам, финальный текст — итоговым сообщением (рисует callback).
- шаг-«контейнер» свернут (default_open=False), поэтому лента сверху
  показывает только вопрос и финальный ответ.

Важный факт (подтверждён шпионским прогоном): у пары
on_chat_model_start/on_llm_end — один и тот же run_id (один run на вызов
LLM). Экземпляр трасера живёт ровно на один on_message, поэтому контейнер
всегда один.
"""

from __future__ import annotations

import json
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

# type шага-«контейнера» процесса ответа (chainlit TrueStepType)
_CONTAINER_TYPE = "run"


class AgentTracer(AsyncBaseTracer):
    """Трасит один агентский цикл и рисует step-иерархию процесса ответа."""

    def __init__(self) -> None:
        super().__init__()
        self._context = context_var.get()
        # Шаг-«контейнер» процесса ответа — один на весь цикл (лениво).
        self._container: Step | None = None
        # run_id вызова llm -> накопленный reasoning-контент
        self._reasoning: dict[str, str] = {}
        # run_id tool-рани -> один Step «имя tool» (выполняется -> результат)
        self._tool_steps: dict[str, Step] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_context(self) -> None:
        context_var.set(self._context)

    async def _container_step(self) -> Step:
        """Шаг-«контейнер» процесса ответа (создаётся один раз)."""
        if self._container is not None:
            return self._container
        step = Step(
            name="процесс ответа",
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
                # language НЕ ставим: chainlit при заданном language рендерит
                # output код-блоком с подсветкой (сырой markdown), а при None —
                # как markdown (GFM-таблицы отрисовываются таблицами).
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
        except Exception:
            return
        message = Message(content=title or "", elements=[element])
        # top-level: без обнуления parent chainlit повесит сообщение ребёнком
        # run-step'а @cl.on_message и график всплывёт над контейнером процесса
        message.parent_id = None
        await message.send()

    # ------------------------------------------------------------------
    # LLM events
    # ------------------------------------------------------------------

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
        # reasoning-токены: ReasoningChatOpenAI кладёт их в
        # additional_kwargs["reasoning_content"] чанка (штатный ChatOpenAI
        # это поле выбрасывает); провайдер-специфичные классы могут отдавать
        # атрибутом reasoning_content — поддерживаем оба места
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

        # Единственное, что рисуем из llm-события — thinking (рассуждения
        # модели). Сам ответ в процессе не показываем: решение вызвать
        # инструменты видно по tool-шагам, а финальный текст пользователь
        # видит итоговым сообщением (callback стримит его).
        # Порядок источников: накопленное из чанков -> атрибут (провайдер-
        # специфичные классы) -> additional_kwargs (ReasoningChatOpenAI;
        # при стриме langchain мержит kwargs чанков конкатенацией строк).
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

    # ------------------------------------------------------------------
    # Tool events
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Не используем run-based обработчики AsyncBaseTracer
    # ------------------------------------------------------------------

    def _persist_run(self, run: Any) -> None:
        pass


def _render_args(args: dict[str, Any] | None) -> str:
    if not args:
        return ""
    return json.dumps(args, ensure_ascii=False, indent=2)
