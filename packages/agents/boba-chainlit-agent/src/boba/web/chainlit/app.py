"""Chainlit entrypoint."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar, cast

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
from boba.web.chainlit.auth import User
from boba.web.chainlit.bootstrap import app_state
from boba.web.chainlit.bridge import ChainlitBridgeSink
from boba.web.chainlit.data_layer import WorkspaceOwnership, WorkspaceOwnershipEntry
from boba.web.chainlit.files import save_upload
from boba.web.chainlit.modesl import ThreadId, UserId
from boba.workspace.contract import WorkspaceId, new_workspace_id
from chainlit.context import local_steps
from chainlit.types import ThreadDict
from chainlit.utils import utc_now

logger = logging.getLogger(__name__)


@cl.password_auth_callback
async def password_auth(username: str, password: str) -> cl.User | None:
    user = app_state().authenticate_user.execute(username, password)
    if user is None:
        return None
    return cl.User(identifier=user.username)


@cl.data_layer
def get_data_layer():
    return app_state().data_layer


def _current_user() -> User:
    """Достаём авторизованного пользователя из Chainlit-сессии."""
    cl_user = cl.context.session.user
    if cl_user is None:
        msg = "Chainlit session has no authenticated user"
        raise RuntimeError(msg)
    return User(username=cl_user.identifier)


class _WarmupTasks:
    """Strong-refs fire-and-forget warmup-тасков.

    Event loop держит таски только weak-reference'ами — без этого set
    их может собрать GC до завершения.
    """

    _TASKS: ClassVar[set[asyncio.Task]] = set()

    @classmethod
    def spawn(cls, user: User, workspace_id: WorkspaceId) -> asyncio.Task:
        task = asyncio.create_task(
            app_state().open_chat_session.execute(user, workspace_id),
        )
        cls._TASKS.add(task)
        task.add_done_callback(lambda t: cls._on_done(t, workspace_id))
        return task

    @classmethod
    def _on_done(cls, task: asyncio.Task, workspace_id: WorkspaceId) -> None:
        cls._TASKS.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.exception(
                "ChatSession warmup failed for workspace=%s",
                workspace_id,
                exc_info=exc,
            )


class _ProfileBuilder:
    """Сборка cl.ChatProfile: один пункт + список workspace'ов пользователя."""

    NEW_SENTINEL: ClassVar[str] = "__new__"
    _LABEL_MAX: ClassVar[int] = 60

    @staticmethod
    async def build(
        ownership: WorkspaceOwnership,
        user_id: UserId,
    ) -> list[cl.ChatProfile]:
        profiles: list[cl.ChatProfile] = [
            cl.ChatProfile(
                name=_ProfileBuilder.NEW_SENTINEL,
                markdown_description="**+ Новый проект** — создать пустой workspace.",
            ),
        ]
        entries = await ownership.list_for_user(user_id)
        for entry in entries:
            profiles.append(
                cl.ChatProfile(
                    name=entry.workspace_id,
                    markdown_description=_ProfileBuilder._render_description(entry),
                ),
            )
        return profiles

    @staticmethod
    def resolve_workspace(profile: str | None) -> WorkspaceId:
        if profile is None or profile == _ProfileBuilder.NEW_SENTINEL:
            return new_workspace_id()
        return WorkspaceId(profile)

    @staticmethod
    def _render_description(entry: WorkspaceOwnershipEntry) -> str:
        heading = (
            _ProfileBuilder._trim(entry.first_user_message)
            if entry.first_user_message
            else entry.workspace_id[:8]
        )
        return f"**{heading}**\n\n_последняя активность: {entry.last_used_at}_"

    @staticmethod
    def _trim(text: str) -> str:
        cleaned = text.strip()
        if len(cleaned) > _ProfileBuilder._LABEL_MAX:
            return cleaned[: _ProfileBuilder._LABEL_MAX] + "…"
        return cleaned


@cl.set_chat_profiles
async def chat_profiles(user: cl.User | None) -> list[cl.ChatProfile]:
    only_new = [
        cl.ChatProfile(
            name=_ProfileBuilder.NEW_SENTINEL,
            markdown_description="**+ Новый проект**",
        ),
    ]
    if user is None:
        return only_new
    state = app_state()
    persisted = await state.data_layer.get_user(ThreadId.from_wire(user.identifier))
    if persisted is None:
        # Пользователь в auth есть, но ещё не закоммитился в users.json
        # (первый login без сообщений) — workspace'ов точно нет.
        return only_new
    return await _ProfileBuilder.build(
        state.workspace_ownership, UserId.from_wire(persisted.id)
    )


@cl.on_chat_start
async def on_chat_start() -> None:
    # Pull-модель: callback только фиксирует выбор workspace_id.
    # Сам ChatSession создаётся лениво в on_message через OpenChatSession под локом,
    # поэтому race condition с fire-and-forget невозможен по построению.
    profile = cast("str | None", cl.user_session.get("chat_profile"))
    workspace_id = _ProfileBuilder.resolve_workspace(profile)
    cl.user_session.set("workspace_id", workspace_id)
    # Прогрев пула в фоне: к моменту первого on_message сессия часто уже готова.
    _WarmupTasks.spawn(_current_user(), workspace_id)


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict) -> None:
    # При клике по thread'у в sidebar Chainlit зовёт нас вместо on_chat_start.
    # Workspace_id живёт в thread.metadata, кладём в user_session — дальше
    # on_message идёт через тот же OpenChatSession.
    metadata = thread.get("metadata") or {}
    raw = metadata.get("workspace_id")
    if not isinstance(raw, str):
        await cl.Message(
            content="Не удалось восстановить workspace для этого чата.",
            author="system",
        ).send()
        logger.error("on_chat_resume: thread %s без workspace_id", thread.get("id"))
        return
    try:
        workspace_id = WorkspaceId(raw)
    except ValueError:
        await cl.Message(
            content="Не удалось восстановить workspace для этого чата.",
            author="system",
        ).send()
        logger.error(
            "on_chat_resume: thread %s имеет невалидный workspace_id=%r",
            thread.get("id"),
            raw,
        )
        return
    cl.user_session.set("workspace_id", workspace_id)
    _WarmupTasks.spawn(_current_user(), workspace_id)


@cl.on_chat_end
async def on_chat_end() -> None:
    # На итерации 2 lifecycle pool'а ещё простой: сессии живут до рестарта процесса.
    # Eviction подключим в итерации 5 (LRU/TTL).
    pass


@cl.on_message
async def on_message(message: cl.Message) -> None:
    workspace_id = cast("WorkspaceId | None", cl.user_session.get("workspace_id"))
    if workspace_id is None:
        await cl.Message(
            content="Сессия не инициализирована. Обновите страницу.",
            author="system",
        ).send()
        logger.error(
            "on_message без workspace_id; chat_profile=%r",
            cl.user_session.get("chat_profile"),
        )
        return
    session = await app_state().open_chat_session.execute(
        _current_user(),
        workspace_id,
    )

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
    """Финальный output step через streaming API: stream_token заменяет содержимое."""
    if is_error:
        step.is_error = True
    await step.stream_token(content, is_sequence=True)


class _EventRenderer:
    """Рендерит AgentEvent'ы в Chainlit UI поверх семей событий.

    Корневая причина «ответ сверху над thinking/tool»:
    `@cl.on_message` оборачивает handler в неявный run-`cl.Step`. `cl.Message`
    в `__post_init__` читает `local_steps` и автоматически получает этот run
    как parent. `cl.Step` так не делает — `parent_id=None` остаётся.
    В итоге answer оказывается ВНУТРИ run-step, а thinking/tool — top-level
    рядом, и frontend рендерит answer «выше» (он внутри parent).

    Фикс: захватываем id текущего run-step и пробрасываем его в parent_id
    каждого cl.Step (thinking/tool/status). Тогда все элементы turn'а
    становятся children одного run, frontend помещает их в parent.steps
    в порядке прихода — порядок гарантирован.
    """

    def __init__(self, parent_id: str | None) -> None:
        self._parent_id = parent_id
        self.answer_msg: cl.Message | None = None
        self.thinking_step: cl.Step | None = None
        # Status — cl.Step(type="run"), а не cl.Message: статус не должен
        # занимать слот в основной chat-ленте и влиять на ordering.
        self.status_step: cl.Step | None = None
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
        if self.status_step is None:
            self.status_step = cl.Step(
                name=text,
                type="run",
                parent_id=self._parent_id,
            )
            await self.status_step.send()
        else:
            self.status_step.name = text
            await self.status_step.update()

    async def _clear_status(self) -> None:
        if self.status_step is not None:
            await self.status_step.remove()
            self.status_step = None

    async def _open_answer(self) -> cl.Message:
        """Создаёт answer-cl.Message по требованию (lazy)."""
        await self._clear_status()
        if self.answer_msg is None:
            # created_at фиксируем в момент Python-создания, чтобы chainlit
            # frontend сортировал answer строго ПОСЛЕ ранее созданных
            # cl.Step (которые ставят created_at в __init__).
            self.answer_msg = cl.Message(content="", created_at=utc_now())
            await self.answer_msg.send()
        return self.answer_msg

    async def _open_thinking(self) -> cl.Step:
        """Создаёт thinking-cl.Step по требованию (lazy)."""
        await self._clear_status()
        if self.thinking_step is None:
            self.thinking_step = cl.Step(
                name="thinking",
                type="run",
                parent_id=self._parent_id,
            )
            await self.thinking_step.send()
        return self.thinking_step

    async def _drop_pending_answer(self) -> None:
        """Сбрасывает ссылку на не-завершённый answer; пустой удаляем из timeline.

        Why: AnswerSource (LLM) эмитит AnswerStarted на ЛЮБОЙ первый delta.content,
        даже если итерация в итоге ушла в tool_calls без финального ContentSnapshot.
        Без сброса между итерациями новый ContentDelta(ANSWER) будет писать в
        старое сообщение из прошлой итерации — оно остаётся выше последующих
        thinking/tool в timeline.
        How to apply: на IterationStarted; если answer_msg пустой — удаляем,
        чтобы фантомное сообщение не висело в чате.
        """
        if self.answer_msg is None:
            return
        if not (self.answer_msg.content or "").strip():
            await self.answer_msg.remove()
        self.answer_msg = None

    async def _on_delta(self, e: ContentDelta) -> None:
        chunk = e.chunk()
        if not chunk:
            return
        match e.slot():
            case SlotKind.ANSWER | SlotKind.REFUSAL:
                msg = await self._open_answer()
                await msg.stream_token(chunk)
            case SlotKind.THINKING:
                step = await self._open_thinking()
                await step.stream_token(chunk)
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
                    created_at=utc_now(),
                ).send()

    async def _on_phase(self, e: PhaseTransition) -> None:
        # Диспатч по классу — рендерим только phase'ы с UI-эффектом.
        # UI-сущности (answer cl.Message, thinking cl.Step) создаём ЛЕНИВО
        # в _on_delta при первом реальном chunk: phase-event может прилетать
        # до контента или вовсе без контента (LLM ушёл в tool_calls после
        # пустого AnswerStarted), а pre-emptive send() ломает порядок в timeline.
        match e:
            case ToolCallStreamStarted():
                await self._on_tool_call_stream_started(e)
            case ToolExecutionStarted():
                await self._on_tool_exec_started(e)
            case LLMRequestSent():
                await self._set_status(f"`{e.model}` sent message")
            case IterationStarted():
                # Между итерациями обнуляем ссылки на answer/thinking,
                # иначе stream_token из новой итерации пишет в UI из старой.
                await self._drop_pending_answer()
                self.thinking_step = None
                await self._set_status(
                    f"Iterable: {e.iteration}/{e.max_iterations}",
                )
            case AnswerStarted() | ThinkingStarted():
                await self._clear_status()
            case GenerationStarted():
                await self._set_status("llm recieved first chunk...")
            case GenerationDone():
                await self._clear_status()

    async def _on_tool_call_stream_started(self, e: ToolCallStreamStarted) -> None:
        await self._clear_status()
        step = cl.Step(name=e.tool_name, type="tool", parent_id=self._parent_id)
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
            created_at=utc_now(),
        ).send()

    async def _on_terminal(self, e: Terminal) -> None:
        await self._clear_status()
        body = e.body() or ""
        await cl.Message(
            content=f"**{e.headline()}**\n\n{body}",
            author="system",
        ).send()


def _current_run_step_id() -> str | None:
    """id текущего run-step (обёртка @cl.on_message вокруг handler'а)."""
    stack = local_steps.get() or []
    return stack[-1].id if stack else None


async def _render_events(queue: asyncio.Queue[AgentEvent | None]) -> None:
    renderer = _EventRenderer(parent_id=_current_run_step_id())
    while True:
        event = await queue.get()
        if event is None:
            break
        try:
            await renderer.handle(event)
        except Exception:
            logger.exception("error rendering event %r", event)
