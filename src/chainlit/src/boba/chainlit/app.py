"""Chainlit entrypoint."""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

import chainlit as cl
from boba.chainlit.bridge import ChainlitBridgeSink
from boba.chainlit.config import load_models
from boba.chainlit.files import save_upload
from boba.chainlit.session import ChatSession
from boba.domain.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerDiscarded,
    AnswerStarted,
    AnswerToken,
    BaseEvent,
    GenerationDone,
    GenerationStarted,
    IterationStarted,
    LLMRequestSent,
    RefusalComplete,
    RefusalToken,
    StageCompleted,
    StageStarted,
    TerminalFailure,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolCallComplete,
    ToolCallFormatFailed,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
    UserNoticeReady,
    UserQueryReceived,
)
from boba.domain.core.patterns import FirstMatchDispatcher, Specification
from boba.domain.core.workspace import WorkspaceId
from chainlit.input_widget import Select

logger = logging.getLogger(__name__)


@functools.cache
def _get_session() -> ChatSession:
    return ChatSession()


@cl.on_chat_start
async def on_chat_start() -> None:
    workspace_id = WorkspaceId.new()
    cl.user_session.set("workspace_id", workspace_id)

    await asyncio.to_thread(_get_session)

    models = load_models()
    if not models:
        msg = "[chainlit] models пуст или не задан — UI не может выбрать модель"
        raise RuntimeError(msg)
    await cl.ChatSettings(
        [
            Select(
                id="model",
                label="LLM модель",
                values=models,
                initial_index=0,
            ),
        ],
    ).send()

    cl.user_session.set("model", models[0])

    await cl.Message(
        content=f"Сессия готова. workspace_id = `{workspace_id.to_wire()}`",
        author="system",
    ).send()


@cl.on_settings_update
async def on_settings_update(settings: dict[str, object]) -> None:
    model = settings.get("model")
    if isinstance(model, str) and model:
        cl.user_session.set("model", model)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    workspace_id = cast(WorkspaceId, cl.user_session.get("workspace_id"))
    model = cast(str, cl.user_session.get("model"))
    session = _get_session()

    saved: list[str] = []
    if message.elements:
        shell = session.project_workspace(workspace_id)
        for el in message.elements:
            src_path = getattr(el, "path", None)
            name = getattr(el, "name", None)
            if not src_path or not name:
                continue
            rel = await asyncio.to_thread(save_upload, shell, src_path, name)
            saved.append(rel)

    query = message.content
    if saved:
        listing = ", ".join(saved)
        query = f"{query}\n\n[attached files in workspace root: {listing}]"

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
    bridge = ChainlitBridgeSink(loop, queue)

    def _run_agent() -> None:
        try:
            session.run(workspace_id, query, bridge, model=model)
        finally:
            bridge.close()

    await asyncio.gather(
        asyncio.to_thread(_run_agent),
        _render_events(queue),
    )


class _IsType(Specification[AgentEvent]):
    """isinstance-предикат. ``IsInstance`` из patterns.py — только для Exception."""

    def __init__(self, *types: type[BaseEvent]) -> None:
        self._types = types

    def check(self, candidate: AgentEvent) -> bool:
        return isinstance(candidate, self._types)


def _llm_request_status(event: LLMRequestSent) -> str:
    tools_hint = " с tools" if event.has_tools else ""
    return (
        f"Жду ответ от модели `{event.model}`{tools_hint}… "
        f"(сообщений в контексте: {event.messages_count})"
    )


async def _finalize_step(
    step: cl.Step, content: str, *, is_error: bool = False,
) -> None:
    """Проставляет финальный output у step через streaming API.

    ``stream_token(..., is_sequence=True)`` заменяет содержимое output
    целиком — эквивалент ``step.output = content``, но через публичный
    streaming-канал. Прямое присваивание ``step.output = ...``
    некорректно типизировано в текущей версии chainlit (property +
    setter + class-level annotation сбивают Pylance).
    """
    if is_error:
        step.is_error = True
    await step.stream_token(content, is_sequence=True)


class _EventRenderer:
    """Рендерит AgentEvent'ы в Chainlit UI.

    Живёт один инстанс на запрос. Хранит:
    - ``answer_msg`` — открытое message'о для стриминга ответных токенов;
    - ``thinking_step`` — активный reasoning-step;
    - ``status_msg`` — transient-индикатор прогресса между этапами;
    - ``tool_steps_by_*`` — маппинги активных tool-steps для связи
      streaming-дельт и результатов.

    ``tool_args_buf`` не нужен — chainlit сам аккумулирует input-токены
    при вызове ``step.stream_token(..., is_input=True)``.
    """

    def __init__(self) -> None:
        self.answer_msg: cl.Message | None = None
        self.thinking_step: cl.Step | None = None
        self.status_msg: cl.Message | None = None
        self.tool_steps_by_index: dict[int, cl.Step] = {}
        self.tool_steps_by_id: dict[str, cl.Step] = {}

    async def _set_status(self, text: str) -> None:
        if self.status_msg is None:
            self.status_msg = cl.Message(content=text, author="system")
            await self.status_msg.send()
        else:
            self.status_msg.content = text
            await self.status_msg.update()

    async def _clear_status(self) -> None:
        if self.status_msg is not None:
            await self.status_msg.remove()
            self.status_msg = None

    async def _open_answer(self) -> cl.Message:
        await self._clear_status()
        if self.answer_msg is None:
            self.answer_msg = cl.Message(content="")
            await self.answer_msg.send()
        return self.answer_msg

    async def on_answer_started(self, event: AnswerStarted) -> None:
        del event
        await self._open_answer()

    async def on_answer_token(self, event: AnswerToken) -> None:
        msg = await self._open_answer()
        await msg.stream_token(event.token)

    async def on_answer_discarded(self, event: AnswerDiscarded) -> None:
        # Middleware переосмыслил поток как tool call — стираем то,
        # что успели нарисовать как answer.
        del event
        if self.answer_msg is not None:
            self.answer_msg.content = ""
            await self.answer_msg.update()
            self.answer_msg = None

    async def on_answer_complete(self, event: AnswerComplete) -> None:
        # Токены уже в UI через stream_token; перезапись content'а
        # вызвала бы полную перерисовку (мигание).
        del event
        self.answer_msg = None

    async def on_thinking_started(self, event: ThinkingStarted) -> None:
        del event
        await self._clear_status()
        # Без явного input-значения chainlit показывает пустой input
        # блок — стартовая инициализация не нужна.
        self.thinking_step = cl.Step(name="thinking", type="run")
        await self.thinking_step.send()

    async def on_thinking_token(self, event: ThinkingToken) -> None:
        if self.thinking_step is not None:
            await self.thinking_step.stream_token(event.token)

    async def on_thinking_complete(self, event: ThinkingComplete) -> None:
        del event
        self.thinking_step = None

    async def on_tool_begin(self, event: ToolCallBegin) -> None:
        await self._clear_status()
        step = cl.Step(name=event.tool_name, type="tool")
        await step.send()
        self.tool_steps_by_index[event.index] = step
        self.tool_steps_by_id[event.tool_call_id] = step

    async def on_tool_arg_delta(self, event: ToolCallArgumentDelta) -> None:
        # Chainlit сам аккумулирует input-токены: ручной буфер
        # arguments не нужен.
        step = self.tool_steps_by_index.get(event.index)
        if step is not None:
            await step.stream_token(event.arguments, is_input=True)

    async def on_tool_complete(self, event: ToolCallComplete) -> None:
        del event

    async def on_tool_exec_started(self, event: ToolExecutionStarted) -> None:
        # Без смены индикации медленные tools выглядят как зависший шаг.
        step = self.tool_steps_by_id.get(event.tool_call_id)
        if step is not None:
            await _finalize_step(step, "⏳ выполняется…")

    async def on_tool_result(self, event: ToolResultReady) -> None:
        step = self.tool_steps_by_id.pop(event.tool_call_id, None)
        if step is not None:
            await _finalize_step(step, event.content)

    async def on_tool_exec_failed(self, event: ToolExecutionFailed) -> None:
        step = self.tool_steps_by_id.pop(event.tool_call_id, None)
        if step is not None:
            await _finalize_step(
                step,
                f"[{event.error_kind}] {event.message}",
                is_error=True,
            )

    async def on_tool_format_failed(self, event: ToolCallFormatFailed) -> None:
        await self._clear_status()
        await cl.Message(
            content=(
                f"**Tool call format error** "
                f"`{event.error_kind}`: {event.message}"
            ),
            author="system",
        ).send()

    async def on_notice(self, event: UserNoticeReady) -> None:
        await self._clear_status()
        await cl.Message(
            content=f"**{event.severity}**: {event.message}",
            author="system",
        ).send()

    async def on_refusal_token(self, event: RefusalToken) -> None:
        msg = await self._open_answer()
        await msg.stream_token(event.token)

    async def on_refusal_complete(self, event: RefusalComplete) -> None:
        del event
        self.answer_msg = None

    async def on_stage_started(self, event: StageStarted) -> None:
        del event
        await self._set_status("Готовлю запрос…")

    async def on_stage_completed(self, event: StageCompleted) -> None:
        del event
        await self._clear_status()

    async def on_llm_request_sent(self, event: LLMRequestSent) -> None:
        await self._set_status(_llm_request_status(event))

    async def on_generation_started(self, event: GenerationStarted) -> None:
        del event
        await self._set_status("Модель обрабатывает запрос…")

    async def on_iteration_started(self, event: IterationStarted) -> None:
        # Первую итерацию не маркируем — пользователь только что
        # отправил запрос.
        if event.iteration <= 1:
            return
        await self._set_status(
            f"Итерация {event.iteration}/{event.max_iterations}…"
        )

    async def on_generation_done(self, event: GenerationDone) -> None:
        del event
        await self._clear_status()

    async def on_user_query(self, event: UserQueryReceived) -> None:
        del event

    async def on_terminal_error(self, event: TerminalFailure) -> None:
        await self._clear_status()
        kind = type(event).__name__
        await cl.Message(
            content=f"**{kind}** `{event.error_kind}`: {event.message}",
            author="system",
        ).send()

    async def on_unknown(self, event: AgentEvent) -> None:
        logger.warning("unhandled agent event: %r", event)


_RenderRoute = Callable[[AgentEvent], Awaitable[None]]


def _build_dispatcher(r: _EventRenderer) -> FirstMatchDispatcher[AgentEvent, Any]:
    # Handler'ы типизированы узкими подклассами AgentEvent; Callable
    # контравариантен по аргументу, поэтому нужен cast. Безопасность
    # держится на _IsType(T) — в маршрут попадает только T.
    def route(
        event_type: type[BaseEvent],
        handler: Callable[..., Awaitable[None]],
    ) -> tuple[Specification[AgentEvent], _RenderRoute]:
        return (_IsType(event_type), cast(_RenderRoute, handler))

    routes: list[tuple[Specification[AgentEvent], _RenderRoute]] = [
        route(AnswerStarted, r.on_answer_started),
        route(AnswerToken, r.on_answer_token),
        route(AnswerDiscarded, r.on_answer_discarded),
        route(AnswerComplete, r.on_answer_complete),
        route(ThinkingStarted, r.on_thinking_started),
        route(ThinkingToken, r.on_thinking_token),
        route(ThinkingComplete, r.on_thinking_complete),
        route(ToolCallBegin, r.on_tool_begin),
        route(ToolCallArgumentDelta, r.on_tool_arg_delta),
        route(ToolCallComplete, r.on_tool_complete),
        route(ToolExecutionStarted, r.on_tool_exec_started),
        route(ToolResultReady, r.on_tool_result),
        route(ToolExecutionFailed, r.on_tool_exec_failed),
        route(ToolCallFormatFailed, r.on_tool_format_failed),
        route(UserNoticeReady, r.on_notice),
        route(RefusalToken, r.on_refusal_token),
        route(RefusalComplete, r.on_refusal_complete),
        route(StageStarted, r.on_stage_started),
        route(StageCompleted, r.on_stage_completed),
        route(IterationStarted, r.on_iteration_started),
        route(LLMRequestSent, r.on_llm_request_sent),
        route(GenerationStarted, r.on_generation_started),
        route(GenerationDone, r.on_generation_done),
        route(UserQueryReceived, r.on_user_query),
        route(TerminalFailure, r.on_terminal_error),
    ]
    return FirstMatchDispatcher[AgentEvent, Any](routes, r.on_unknown)


async def _render_events(queue: asyncio.Queue[AgentEvent | None]) -> None:
    renderer = _EventRenderer()
    dispatch = _build_dispatcher(renderer)

    while True:
        event = await queue.get()
        if event is None:
            break
        await dispatch(event)
