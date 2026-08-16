"""Рассылка событий хода во все живые сокеты треда.

Ход живёт дольше сокета: вкладку обновили, а стрим продолжается. ThreadEmitter
решает адресатов в момент эмиссии — каждое событие уходит всем живым сессиям
треда, новая вкладка подхватывает стрим сразу после подключения.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from chainlit.context import ChainlitContext, context_var, get_context
from chainlit.emitter import ChainlitEmitter
from chainlit.server import sio
from chainlit.session import WebsocketSession, ws_sessions_id

__all__ = ["StickyLoadingEmitter", "ThreadEmitter", "ThreadRoom"]

logger = logging.getLogger(__name__)


class StickyLoadingEmitter(ChainlitEmitter):
    """Эмиттер resume-хендлера: не даёт chainlit погасить индикатор хода.

    wrap_user_function(with_task=True) шлёт task_end сразу после хендлера;
    при живом ходе loading обязан пережить resume, поэтому task_end глушится.
    """

    async def task_end(self) -> None:
        return None


class ThreadEmitter(ChainlitEmitter):
    """Эмиттер треда: события уходят каждому живому сокету, а не одной сессии."""

    def __init__(self, session: WebsocketSession, thread_id: str) -> None:
        super().__init__(session)
        self._thread_id = thread_id

    @property
    def emit(self) -> Callable[[str, Any], Awaitable[None]]:
        async def emit_to_thread(event: str, data: Any) -> None:
            for session in ThreadRoom.sessions(self._thread_id):
                # одна битая сессия не должна ронять весь ход
                try:
                    await cast("Awaitable[None]", session.emit(event, data))
                except Exception:
                    logger.warning(
                        "emit %s failed for session %s of thread %s",
                        event,
                        session.id,
                        self._thread_id,
                        exc_info=True,
                    )

        return emit_to_thread

    @property
    def emit_call(self) -> Any:
        sessions = ThreadRoom.sessions(self._thread_id)
        if self.session in sessions:
            return self.session.emit_call
        if sessions:
            return sessions[0].emit_call
        return self.session.emit_call


class ThreadRoom:
    """Живые сессии треда и переключение контекста хода на рассылку."""

    @staticmethod
    def sessions(thread_id: str) -> list[WebsocketSession]:
        """Сессии треда с живым сокетом; умершие ждут таймаута chainlit."""
        sessions: list[WebsocketSession] = []
        for session in list(ws_sessions_id.values()):
            if session.thread_id != thread_id:
                continue
            if not sio.manager.is_connected(session.socket_id, "/"):
                continue
            sessions.append(session)
        return sessions

    @staticmethod
    def activate(thread_id: str) -> None:
        """Подменяет контекст текущей задачи: эмиссии хода видят все вкладки."""
        current = get_context()
        session = cast("WebsocketSession", current.session)
        emitter = ThreadEmitter(session, thread_id)
        context_var.set(ChainlitContext(session, emitter))
        logger.info("broadcast on thread %s: session=%s", thread_id, session.id)

    @staticmethod
    def keep_loading() -> None:
        """Глушит task_end обёртки chainlit вокруг текущего хендлера."""
        current = get_context()
        session = cast("WebsocketSession", current.session)
        context_var.set(ChainlitContext(session, StickyLoadingEmitter(session)))
