"""Chainlit entrypoint."""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import cast

import chainlit as cl
from boba.domain.agent.events import (
    Advisory,
    AgentEvent,
    AnswerStarted,
    ContentDelta,
    ContentSnapshot,
    GenerationDone,
    GenerationStarted,
    IterationStarted,
    LLMRequestSent,
    PhaseTransition,
    SlotKind,
    Terminal,
    ThinkingStarted,
    ToolCallStreamStarted,
    ToolExecutionFailed,
    ToolExecutionStarted,
)
from boba.domain.core.workspace import WorkspaceId
from boba.web.chainlit.bridge import ChainlitBridgeSink
from boba.web.chainlit.files import save_upload
from boba.web.chainlit.session import ChatSession
from chainlit.input_widget import Select

logger = logging.getLogger(__name__)


@functools.cache
def _get_session() -> ChatSession:
    return ChatSession()


@cl.on_chat_start
async def on_chat_start() -> None:
    workspace_id = WorkspaceId.new()
    cl.user_session.set("workspace_id", workspace_id)

    session = await asyncio.to_thread(_get_session)

    models = session.models
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


async def _finalize_step(
    step: cl.Step,
    content: str,
    *,
    is_error: bool = False,
) -> None:
    """Проставляет финальный output у step через streaming API.

    stream_token(..., is_sequence=True) заменяет содержимое output
    целиком — эквивалент step.output = content, но через публичный
    streaming-канал. Прямое присваивание step.output = ...
    некорректно типизировано в текущей версии chainlit (property +
    setter + class-level annotation сбивают Pylance).
    """
    if is_error:
        step.is_error = True
    await step.stream_token(content, is_sequence=True)


class _EventRenderer:
    """Рендерит AgentEvent'ы в Chainlit UI поверх семей событий.

    Sink строится вокруг семей: ContentDelta → стримим в нужный слот
    через slot() / chunk(); ContentSnapshot для streaming-
    слотов закрывает сообщение, для tool-слотов рендерит результат;
    PhaseTransition — статус-индикатор; Advisory/Terminal —
    system-сообщения.

    Concrete-типы используются точечно в двух местах: чтобы знать,
    какой LLMToolCall сейчас исполняется (ToolExecutionStarted)
    и для ToolExecutionFailed (нужен call.id для матчинга со
    Step-ом). Это контролируемое нарушение «sink не знает concrete» —
    chainlit делает rich-UI, ему нужны структурированные поля.
    """

    def __init__(self) -> None:
        self.answer_msg: cl.Message | None = None
        self.thinking_step: cl.Step | None = None
        self.status_msg: cl.Message | None = None
        # tool_call_id → Step. index для нас не интересен —
        # все события несут tool_call_id уже на уровне семей.
        self.tool_steps_by_id: dict[str, cl.Step] = {}

    async def handle(self, event: AgentEvent) -> None:
        match event:
            case ContentDelta():
                await self._on_delta(event)
            case ContentSnapshot():
                await self._on_snapshot(event)
            case Terminal():
                await self._on_terminal(event)
            case Advisory():
                await self._on_advisory(event)
            case PhaseTransition():
                await self._on_phase(event)

    # ── Status helpers ───────────────────────────────────────────────

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

    # ── Family handlers ──────────────────────────────────────────────

    async def _on_delta(self, e: ContentDelta) -> None:
        chunk = e.chunk()
        if not chunk:
            return
        match e.slot():
            case SlotKind.ANSWER | SlotKind.REFUSAL:
                msg = await self._open_answer()
                await msg.stream_token(chunk)
            case SlotKind.THINKING:
                if self.thinking_step is not None:
                    await self.thinking_step.stream_token(chunk)
            case SlotKind.TOOL_ARGS:
                step = self.tool_steps_by_id.get(e.slot_id())
                if step is not None:
                    await step.stream_token(chunk, is_input=True)
            case _:
                pass

    async def _on_snapshot(self, e: ContentSnapshot) -> None:
        slot = e.slot()
        match slot:
            case SlotKind.ANSWER | SlotKind.REFUSAL:
                # Токены уже отрисованы потоково; перезапись content'а
                # вызвала бы полную перерисовку (мигание). Просто
                # закрываем активный message.
                self.answer_msg = None
            case SlotKind.THINKING:
                self.thinking_step = None
            case SlotKind.TOOL_RESULT:
                step = self.tool_steps_by_id.pop(e.slot_id(), None)
                if step is not None:
                    await _finalize_step(step, e.body())
            case SlotKind.TOOL_CALL:
                # Step уже создан в ToolCallStreamStarted; args стримятся
                # через ContentDelta. Здесь — нечего делать (всё уже в UI).
                pass
            case SlotKind.USER_QUERY:
                # Пользовательский ввод уже виден в чате — chainlit его сам
                # отрисовал из cl.Message; повторное отображение не нужно.
                pass
            case SlotKind.FEEDBACK:
                await cl.Message(
                    content=f"**Feedback to LLM**:\n\n{e.body()}",
                    author="system",
                ).send()

    async def _on_phase(self, e: PhaseTransition) -> None:
        # Большинство phase-событий — внутренний пульс агента, в UI их
        # не показываем. Здесь — только те, у которых есть рендер-
        # эффект; диспатч строго по классу, не по label() (label —
        # human-readable текст для логов и status-bar, не discriminator).
        # Не покрытые здесь GenerationRetried / LLMResponseStreamOpened /
        # IterationStarted (первая итерация) — наблюдаемого эффекта нет.
        match e:
            case ToolCallStreamStarted():
                await self._on_tool_call_stream_started(e)
            case ToolExecutionStarted():
                await self._on_tool_exec_started(e)
            case LLMRequestSent():
                tools_hint = " с tools" if e.has_tools else ""
                await self._set_status(
                    f"Жду ответ от модели `{e.model}`{tools_hint}… "
                    f"(сообщений в контексте: {e.messages_count})",
                )
            case IterationStarted() if e.iteration > 1:
                # Первую итерацию не маркируем — пользователь только что
                # отправил запрос. Дальнейшие — статус-строка.
                await self._set_status(
                    f"Итерация: {e.iteration}/{e.max_iterations}…",
                )
            case AnswerStarted():
                await self._open_answer()
            case ThinkingStarted():
                await self._clear_status()
                self.thinking_step = cl.Step(name="thinking", type="run")
                await self.thinking_step.send()
            case GenerationStarted():
                await self._set_status("Модель обрабатывает запрос…")
            case GenerationDone():
                await self._clear_status()

    async def _on_tool_call_stream_started(self, e: ToolCallStreamStarted) -> None:
        await self._clear_status()
        step = cl.Step(name=e.tool_name, type="tool")
        await step.send()
        self.tool_steps_by_id[e.tool_call_id] = step

    async def _on_tool_exec_started(self, e: ToolExecutionStarted) -> None:
        # Без смены индикации медленные tools выглядят как зависший шаг.
        step = self.tool_steps_by_id.get(e.call.id)
        if step is not None:
            await _finalize_step(step, "⏳ выполняется…")

    async def _on_advisory(self, e: Advisory) -> None:
        await self._clear_status()
        # ToolExecutionFailed — особый случай: финализируем соответствующий
        # tool-Step, чтобы пользователь увидел ошибку рядом с вызовом.
        if isinstance(e, ToolExecutionFailed):
            step = self.tool_steps_by_id.pop(e.call.id, None)
            if step is not None:
                await _finalize_step(
                    step,
                    f"[{e.failure.error_kind}] {e.failure.message}",
                    is_error=True,
                )
                return
        body = e.body() or ""
        await cl.Message(
            content=f"**{e.headline()}**\n\n{body}",
            author="system",
        ).send()

    async def _on_terminal(self, e: Terminal) -> None:
        await self._clear_status()
        body = e.body() or ""
        await cl.Message(
            content=f"**{e.headline()}**\n\n{body}",
            author="system",
        ).send()


async def _render_events(queue: asyncio.Queue[AgentEvent | None]) -> None:
    renderer = _EventRenderer()
    while True:
        event = await queue.get()
        if event is None:
            break
        try:
            await renderer.handle(event)
        except Exception:
            logger.exception("error rendering event %r", event)
