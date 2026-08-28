"""Доставка событий чата во все живые вкладки треда: эмиттер рассылки, поверхность
рендерера, уведомления вне хода и транспорт сигналов канваса.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar, cast

from boba.canvas.canvas import CanvasSignal, SignalTransport
from boba.chainlit.chat.feed import TextClip
from boba.chainlit.infra.session import current_session, session_source_ref
from boba.chainlit.rendering.chat_view import ChatView, LiveSink
from boba.chainlit.rendering.renderer import ChatRenderers, RenderSurface
from boba.identity.context import Scope
from boba.identity.errors import InternalServiceError
from boba.identity.locks import LockMode
from boba.identity.run import RunRegistry
from boba.messaging import CanvasChanged, LockToken, Notice, NoticeLevel
from boba.runtime import providers as runtime
from boba.runtime.di import Container
from chainlit.context import ChainlitContext, context_var
from chainlit.emitter import ChainlitEmitter
from chainlit.server import sio
from chainlit.session import WebsocketSession

__all__ = [
    "CanvasRoomTransport",
    "ChatNotices",
    "ChatRoomSurface",
    "StickyLoadingEmitter",
    "ThreadEmitter",
    "ThreadLive",
    "ThreadRoom",
]

logger = logging.getLogger(__name__)


class StickyLoadingEmitter(ChainlitEmitter):
    """Эмиттер обработчика восстановления треда: глушит task_end обёртки chainlit,
    чтобы индикатор живого хода не гас у новой вкладки.
    """

    async def task_end(self) -> None:
        return None


class ThreadEmitter(ChainlitEmitter):
    """Эмиттер, который доставляет событие всем живым вкладкам треда, выбирая
    адресатов при каждой отправке.
    """

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
    """Живые вкладки треда и переключение контекста chainlit на рассылку по ним."""

    @staticmethod
    def sessions(thread_id: str) -> list[WebsocketSession]:
        """Возвращает сессии треда, у которых сокет подключён прямо сейчас."""
        sessions: list[WebsocketSession] = []
        for session in session_source_ref().in_thread(thread_id):
            socket = session.websocket
            if socket is None:
                continue

            if not sio.manager.is_connected(session.socket_id, "/"):
                continue

            sessions.append(socket)

        return sessions

    @staticmethod
    def activate(thread_id: str) -> None:
        """Переключает контекст текущей задачи на рассылку: события хода уходят всем
        вкладкам треда.
        """
        session = ThreadRoom.websocket()
        emitter = ThreadEmitter(session, thread_id)
        context_var.set(ChainlitContext(session, emitter))
        logger.info("broadcast on thread %s: session=%s", thread_id, session.id)

    @staticmethod
    def keep_loading() -> None:
        """Подменяет эмиттер текущего обработчика на StickyLoadingEmitter, чтобы обёртка
        chainlit не погасила индикатор хода.
        """
        session = ThreadRoom.websocket()
        context_var.set(ChainlitContext(session, StickyLoadingEmitter(session)))

    @staticmethod
    def websocket() -> WebsocketSession:
        """Возвращает сокетную сессию текущего вызова; без неё рассылать события некуда,
        и это внутренняя ошибка.
        """
        session = current_session().websocket
        if session is None:
            raise InternalServiceError(
                internal_detail="thread room needs a websocket session",
                user_detail=None,
            )

        return session


class ChatRoomSurface(RenderSurface):
    """Поверхность рендерера: строит контекст chainlit с якорной сессией и шлёт ленту
    и сигналы всем живым вкладкам треда.
    """

    EVENT: ClassVar[str] = "window_message"

    def __init__(self, anchor: WebsocketSession, thread_id: str) -> None:
        self._anchor = anchor
        self._thread_id = thread_id

    def context(self) -> ChainlitContext | None:
        return ChainlitContext(
            self._anchor, ThreadEmitter(self._anchor, self._thread_id)
        )

    async def window_message(self, payload: Mapping[str, Any]) -> None:
        failed: list[str] = []
        for session in ThreadRoom.sessions(self._thread_id):
            # одна битая сессия не должна глушить сигнал остальным
            try:
                await cast("Awaitable[None]", session.emit(self.EVENT, dict(payload)))
            except Exception as exc:
                failed.append(f"{session.id}: {exc}")

        if failed:
            raise InternalServiceError(
                internal_detail=(
                    f"window message failed for sessions of thread {self._thread_id}: "
                    + "; ".join(failed)
                ),
                user_detail="The page did not receive a live update",
            )

    async def task_start(self) -> None:
        await ThreadEmitter(self._anchor, self._thread_id).task_start()

    async def task_end(self) -> None:
        await ThreadEmitter(self._anchor, self._thread_id).task_end()

    @classmethod
    def renderer_of(cls, anchor: WebsocketSession, thread_id: str):
        """Возвращает рендерер треда этого процесса, создавая его при первом
        обращении.
        """
        root = Container.root
        if root is None:
            raise InternalServiceError(
                internal_detail="DI container is not initialised",
                user_detail=None,
            )

        return ChatRenderers.ensure(
            thread_id,
            root.resolved(runtime.message_bus),
            ChatView(thread_id, LiveSink()),
            root.resolved(runtime.payload_store),
            cls(anchor, thread_id),
        )


class ThreadLive:
    """Отвечает, идёт ли ход треда где бы то ни было: в реестре этого процесса или
    под живой монопольной блокировкой другого инстанса.
    """

    @staticmethod
    async def turn_alive(thread_id: str) -> bool:
        if RunRegistry.active(thread_id) is not None:
            return True

        root = Container.root
        if root is None:
            return False

        locks = root.resolved(runtime.live_locks)
        holders = await locks.holders_of(Scope.chat(thread_id))
        exclusive = [holder for holder in holders if holder.mode is LockMode.EXCLUSIVE]

        return bool(exclusive)


class ChatNotices:
    """Публикует уведомление Notice в область треда текущей сессии для действий вне
    хода, чтобы его показал рендерер треда.
    """

    @staticmethod
    async def error(text: str) -> None:
        thread_id = current_session().thread_id
        if thread_id is None:
            raise InternalServiceError(
                internal_detail="notice outside a chainlit thread",
                user_detail=None,
            )

        ChatRoomSurface.renderer_of(ThreadRoom.websocket(), thread_id)
        root = Container.root
        if root is None:
            raise InternalServiceError(
                internal_detail="DI container is not initialised",
                user_detail=None,
            )

        bus = root.resolved(runtime.message_bus)
        notice = Notice(level=NoticeLevel.ERROR, text=TextClip.fit(text))
        await bus.publish(Scope.chat(thread_id), notice, LockToken.local())


class CanvasRoomTransport(SignalTransport):
    """Транспорт сигналов слежения за файлом: публикует CanvasChanged в область
    треда, откуда рендерер шлёт window_message вкладкам.
    """

    def alive(self, thread_id: str) -> bool:
        return bool(ThreadRoom.sessions(thread_id))

    async def send(self, thread_id: str, signal: CanvasSignal) -> None:
        sessions = ThreadRoom.sessions(thread_id)
        if not sessions:
            return

        renderer = ChatRoomSurface.renderer_of(sessions[0], thread_id)
        root = Container.root
        if root is None:
            return

        bus = root.resolved(runtime.message_bus)
        message = CanvasChanged(
            path=signal.path,
            nonce=signal.nonce,
            revision=signal.revision,
            size=signal.size,
            closed=signal.closed,
            note=signal.note,
        )
        logger.debug("canvas signal for thread %s via %s", thread_id, renderer)
        await bus.publish(Scope.chat(thread_id), message, LockToken.local())
