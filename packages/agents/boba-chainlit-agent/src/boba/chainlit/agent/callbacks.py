"""Chainlit-callback'и (entry-file для `run_chainlit`)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import ClassVar, cast

import chainlit as cl
from chainlit.context import local_steps
from chainlit.types import ThreadDict

from boba.agent.events import AgentEvent
from boba.chainlit.agent.models import ThreadId, ThreadMeta, User, UserId
from boba.chainlit.agent.rendering import (
    AgentEventDispatcher,
    ChainlitBridgeSink,
    ChainlitLiveTarget,
)
from boba.chainlit.agent.state import app_state
from boba.chainlit.agent.uploads import save_user_uploads
from boba.workspace.contract import WorkspaceId, new_workspace_id

logger = logging.getLogger(__name__)

_NAME_MAX = 60
"""Max длина имени треда, выведенного из первого user-сообщения."""


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


def _current_thread_id() -> ThreadId:
    """thread_id текущей chainlit-сессии."""
    return ThreadId(cl.context.session.thread_id)


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


async def _ensure_thread_registered(
    thread_id: ThreadId,
    workspace_id: WorkspaceId,
    user_identifier: str,
    first_message: str,
) -> None:
    """Регистрируем тред в репо при первом сообщении.

    on_chat_start этого не делает: пока юзер не написал ни строки,
    в sidebar/на диске не должно появляться пустых записей и workspace'ов.
    Здесь создаём `ThreadMeta` при первом сообщении, либо проставляем
    имя у уже существующей меты, если оно ещё пустое (resume сценарий).
    """
    existing = await app_state().thread_repository.get_meta(thread_id)
    if existing is not None and existing.name:
        return
    name = _trim(first_message) or None
    now = datetime.now(UTC).isoformat()
    if existing is None:
        persisted = await app_state().data_layer.get_user(user_identifier)
        user_id = UserId(persisted.id) if persisted is not None else None
        meta = ThreadMeta(
            id=thread_id,
            workspace_id=workspace_id,
            user_id=user_id,
            user_identifier=user_identifier,
            name=name,
            tags=[],
            metadata={"workspace_id": workspace_id},
            created_at=now,
            updated_at=now,
        )
    else:
        meta = existing.model_copy(update={"name": name, "updated_at": now})
    await app_state().thread_repository.upsert_meta(meta)


def _trim(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) > _NAME_MAX:
        return cleaned[:_NAME_MAX] + "…"
    return cleaned


@cl.on_chat_start
async def on_chat_start() -> None:
    # Каждый chainlit-thread = свой workspace, но создаём его лениво:
    # workspace_id просто резервируем, ThreadMeta и каталоги на диске
    # появятся только при первом on_message. Так пустые "просто открыл
    # приложение" не пачкают sidebar и local/workspaces/.
    workspace_id = new_workspace_id()
    cl.user_session.set("workspace_id", workspace_id)


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict) -> None:
    # При клике по thread'у в sidebar Chainlit зовёт нас вместо on_chat_start.
    # Сообщения уже отрендерены через data_layer.get_thread → HistoryService.
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
    pass


@cl.on_message
async def on_message(message: cl.Message) -> None:
    workspace_id = cast("WorkspaceId | None", cl.user_session.get("workspace_id"))
    if workspace_id is None:
        await cl.Message(
            content="Сессия не инициализирована. Обновите страницу.",
            author="system",
        ).send()
        logger.error("on_message без workspace_id в user_session")
        return

    thread_id = _current_thread_id()
    user = _current_user()
    # Ленивая регистрация: если меты ещё нет — создаём, имя берём из
    # первого сообщения. Делаем до запуска агента, чтобы sidebar
    # обновился сразу.
    await _ensure_thread_registered(
        thread_id, workspace_id, user.username, message.content
    )

    try:
        session = await app_state().open_chat_session.execute(
            user,
            workspace_id,
        )
    except Exception as exc:
        logger.exception(
            "Failed to open chat session for workspace=%s", workspace_id
        )
        await cl.Message(
            content=(
                f"Не удалось открыть сессию: {type(exc).__name__}: {exc}\n\n"
                "Подробности — в логах сервера."
            ),
            author="system",
        ).send()
        return

    query = message.content
    if message.elements:
        uploaded = await save_user_uploads(
            message.elements, session.project_workspace(),
        )
        if uploaded:
            files_block = "\n".join(f"- {p}" for p in uploaded)
            query = (
                f"{message.content}\n\n"
                f"[Прикреплённые файлы (workspace-relative):\n{files_block}]"
            )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
    bridge = ChainlitBridgeSink(loop, queue)

    def _run_agent() -> None:
        try:
            session.run(query, bridge)
        finally:
            bridge.close()

    try:
        await asyncio.gather(
            asyncio.to_thread(_run_agent),
            _render_events(queue),
        )
    except Exception as exc:
        logger.exception(
            "Agent run failed for workspace=%s", workspace_id
        )
        await cl.Message(
            content=(
                f"Ошибка при выполнении агента: {type(exc).__name__}: {exc}\n\n"
                "Подробности — в логах сервера."
            ),
            author="system",
        ).send()


def _current_run_step_id() -> str | None:
    """id текущего run-step (обёртка @cl.on_message вокруг handler'а)."""
    stack = local_steps.get() or []
    return stack[-1].id if stack else None


async def _render_events(queue: asyncio.Queue[AgentEvent | None]) -> None:
    """Потребитель очереди live-событий; маппит через общий диспатчер.

    Toggle `diagnostic_mode` берётся из `cl.user_session` - переключить
    можно из UI через ChatSettings или slash-command (вне scope этого
    файла). По умолчанию False -> диагностика не рендерится.
    """
    diagnostic = bool(cl.user_session.get("diagnostic_mode", False))
    target = ChainlitLiveTarget(
        parent_id=_current_run_step_id(),
        diagnostic=diagnostic,
    )
    dispatcher = AgentEventDispatcher(target)
    while True:
        event = await queue.get()
        if event is None:
            break
        try:
            await dispatcher.handle(event)
        except Exception:
            logger.exception("error rendering event %r", event)
