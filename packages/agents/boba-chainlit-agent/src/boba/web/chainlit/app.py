"""Chainlit entrypoint."""

from __future__ import annotations

import asyncio
import logging
from typing import cast

import chainlit as cl
from boba.agent.events import (
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
from boba.web.chainlit.bootstrap import app_state
from boba.web.chainlit.bridge import ChainlitBridgeSink
from boba.web.chainlit.files import save_upload
from boba.web.chainlit.session import ChatSession
from boba.workspace.contract import WorkspaceId

logger = logging.getLogger(__name__)


def _build_session(workspace_id: WorkspaceId) -> ChatSession:
    state = app_state()
    return ChatSession(
        workspace_id,
        state.make_builder(),
        state.project_workspaces,
        state.history_workspaces,
    )


@cl.on_chat_start
async def on_chat_start() -> None:
    workspace_id = WorkspaceId.from_wire(cl.context.session.thread_id)
    session = await asyncio.to_thread(_build_session, workspace_id)
    cl.user_session.set("workspace_id", workspace_id)
    cl.user_session.set("session", session)

    await cl.Message(
        content=f"Сессия готова. workspace_id = `{workspace_id.to_wire()}`",
        author="system",
    ).send()


@cl.on_chat_end
async def on_chat_end() -> None:
    # Снимаем ссылку на ChatSession — Agent/MessageService собирает GC.
    # project/history shells живут в registry и переживают сессию (это нормально:
    # при resume того же thread'а получим тот же FS-workspace).
    cl.user_session.set("session", None)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    session = cast(ChatSession, cl.user_session.get("session"))

    saved: list[str] = []
    if message.elements:
        shell = session.project_workspace()
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
            session.run(query, bridge)
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
    """
    Финальный output у step через streaming API
    (stream_token заменяет содержимое)
    """
    if is_error:
        step.is_error = True
    await step.stream_token(content, is_sequence=True)


class _EventRenderer:
    """Рендерит AgentEvent'ы в Chainlit UI поверх семей событий."""

    def __init__(self) -> None:
        self.answer_msg: cl.Message | None = None
        self.thinking_step: cl.Step | None = None
        self.status_msg: cl.Message | None = None
        # tool_call_id → Step.
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
                # Токены уже отрисованы потоково — просто закрываем сообщение.
                self.answer_msg = None
            case SlotKind.THINKING:
                self.thinking_step = None
            case SlotKind.TOOL_RESULT:
                step = self.tool_steps_by_id.pop(e.slot_id(), None)
                if step is not None:
                    await _finalize_step(step, e.body())
            case SlotKind.TOOL_CALL:
                # Step создан в ToolCallStreamStarted, args стримятся через ContentDelta
                pass
            case SlotKind.USER_QUERY:
                # chainlit уже отрисовал ввод из cl.Message.
                pass
            case SlotKind.FEEDBACK:
                await cl.Message(
                    content=f"**Feedback to LLM**:\n\n{e.body()}",
                    author="system",
                ).send()

    async def _on_phase(self, e: PhaseTransition) -> None:
        # Диспатч по классу — рендерим только phase'ы с UI-эффектом.
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
                # Первую итерацию не маркируем — пользователь только что отправил запрос
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
        # Меняем индикацию — иначе медленные tools выглядят как зависший шаг.
        step = self.tool_steps_by_id.get(e.call.id)
        if step is not None:
            await _finalize_step(step, "⏳ выполняется…")

    async def _on_advisory(self, e: Advisory) -> None:
        await self._clear_status()
        # ToolExecutionFailed — финализируем tool-Step,
        # чтобы ошибка была рядом с вызовом
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
