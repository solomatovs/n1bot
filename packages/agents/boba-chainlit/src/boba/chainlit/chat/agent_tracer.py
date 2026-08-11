"""Langchain-tracer: процесс ответа одним сворачиваемым шагом через ChatView."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar
from uuid import UUID

from langchain_core.outputs import ChatGenerationChunk, GenerationChunk
from langchain_core.tracers.base import AsyncBaseTracer
from typing_extensions import ParamSpec, override

from boba.chainlit.agent.chat_model import ResponseField
from boba.chainlit.rendering.chat_view import ChatView
from boba.chainlit.rendering.errors import show_error
from boba.chainlit.rendering.stream_view import StreamNote, ToolStreams
from chainlit.context import context_var
from chainlit.step import Step

__all__ = ["AgentTracer"]

logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _visible_failure(
    fn: Callable[_P, Coroutine[Any, Any, _R]],
) -> Callable[_P, Coroutine[Any, Any, _R | None]]:
    """langchain гасит исключения коллбэков в logger.warning: показываем сами."""

    @functools.wraps(fn)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R | None:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            logger.exception("rendering failed in %s", fn.__name__)
            await show_error(f"Failed to render step ({fn.__name__}): {e}")
            return None

    return wrapper


class AgentTracer(AsyncBaseTracer):
    """Трасит один агентский цикл и рисует step-иерархию процесса ответа."""

    def __init__(self, view: ChatView, user_id: str) -> None:
        super().__init__()
        self._context = context_var.get()
        self._view = view
        self._user_id = user_id
        self._reasoning: dict[str, str] = {}
        self._tool_steps: dict[str, Step] = {}
        self._stream_calls: dict[str, str] = {}

    @property
    def view(self) -> ChatView:
        """Лента, в которую трасер пишет шаги."""
        return self._view

    @property
    def pending_tool_steps(self) -> list[Step]:
        """Шаги инструментов, ещё не завершённые ходом."""
        return list(self._tool_steps.values())

    @property
    def pending_reasoning(self) -> str:
        """Рассуждения незавершённых прогонов: их ждёт история после остановки."""
        parts: list[str] = []
        for text in self._reasoning.values():
            parts.append(text)

        return "".join(parts)

    def _set_context(self) -> None:
        context_var.set(self._context)

    @staticmethod
    def _reasoning_of(message: Any) -> str:
        if message is None:
            return ""

        value = getattr(message, "reasoning_content", None)
        if value:
            return str(value)

        extra = getattr(message, "additional_kwargs", None)
        if not extra:
            return ""

        value = extra.get(ResponseField.REASONING_CONTENT.value)
        if value:
            return str(value)

        return ""

    @override
    @_visible_failure
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
        message = getattr(chunk, "message", None)

        if reasoning := self._reasoning_of(message):
            run_key = str(run_id)
            self._reasoning[run_key] = self._reasoning.get(run_key, "") + reasoning
            await self._view.stream_thinking(reasoning, getattr(message, "id", None))

        return await super().on_llm_new_token(
            token,
            chunk=chunk,
            run_id=run_id,
            parent_run_id=parent_run_id,
            **kwargs,
        )

    @override
    @_visible_failure
    async def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        streamed = self._reasoning.pop(str(run_id), "")
        await self._view.close_thinking()

        message: Any = None
        if response.generations and response.generations[0]:
            message = getattr(response.generations[0][0], "message", None)

        # рассуждения без стрима приходят разом в итоговом сообщении
        if not streamed:
            if text := self._reasoning_of(message):
                await self._view.thinking(text, getattr(message, "id", None))

        return await super().on_llm_end(
            response,
            run_id=run_id,
            parent_run_id=parent_run_id,
            **kwargs,
        )

    @override
    @_visible_failure
    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        await self._view.close_thinking()
        return await super().on_llm_error(
            error,
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            **kwargs,
        )

    @override
    @_visible_failure
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
        tool_name = name
        if not tool_name and serialized:
            tool_name = serialized.get("name")
        if not tool_name:
            tool_name = "tool"
        call_id = kwargs.get("tool_call_id")
        call_key: str | None = None
        if call_id:
            call_key = str(call_id)
        self._tool_steps[str(run_id)] = await self._view.tool_started(
            tool_name, inputs, call_key
        )
        self._begin_stream(str(run_id), tool_name, call_key)
        return await super().on_tool_start(
            serialized,
            input_str,
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            metadata=metadata,
            name=name,
            inputs=inputs,
            **kwargs,
        )

    def _begin_stream(self, run_key: str, tool_name: str, call_key: str | None) -> None:
        """Открывает журнал живого вывода вызова.

        В тап рекордер ставит обвязка ToolStreamTapGuard уже в потоке
        инструмента: колбэки sync-тулов langchain гоняет в чужом event loop'е,
        и contextvar отсюда до функции тула не доходит.
        """
        if not call_key:
            return

        if not self._user_id:
            return

        if not ToolStreams.streamable(tool_name):
            return

        stream = ToolStreams.begin(
            self._user_id, self._view.thread_id, call_key, tool_name
        )
        if stream is None:
            return

        self._stream_calls[run_key] = call_key

    def _finish_stream(self, run_key: str, note: StreamNote) -> None:
        call_id = self._stream_calls.pop(run_key, None)
        if call_id is None:
            return

        ToolStreams.finish(self._view.thread_id, call_id, str(note))

    @override
    @_visible_failure
    async def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self._set_context()
        self._finish_stream(str(run_id), StreamNote.FINISHED)
        if step := self._tool_steps.pop(str(run_id), None):
            if getattr(output, "status", None) == "error":
                await self._view.tool_failed(step, getattr(output, "content", output))
            else:
                artifact = getattr(output, "artifact", None)
                if artifact is None:
                    artifact = output
                await self._view.tool_finished(
                    step,
                    artifact,
                    getattr(output, "tool_call_id", None),
                )
        return await super().on_tool_end(output, run_id=run_id, **kwargs)

    @_visible_failure
    async def stop_pending(self, note: str) -> None:
        """Закрыть рассуждения и шаги инструментов, оставшиеся после остановки.

        Накопленный reasoning тут не чистится: сразу после отрисовки его
        забирает история хода.
        """
        self._set_context()
        await self._view.close_thinking()

        while self._stream_calls:
            run_key, _ = next(iter(self._stream_calls.items()))
            self._finish_stream(run_key, StreamNote.STOPPED)

        while self._tool_steps:
            _, step = self._tool_steps.popitem()
            await self._view.tool_stopped(step, note)

    @override
    @_visible_failure
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
        self._finish_stream(str(run_id), StreamNote.FAILED)
        if step := self._tool_steps.pop(str(run_id), None):
            await self._view.tool_failed(step, error)
        return await super().on_tool_error(
            error,
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            **kwargs,
        )

    @override
    async def _persist_run(self, run: Any) -> None:
        pass
