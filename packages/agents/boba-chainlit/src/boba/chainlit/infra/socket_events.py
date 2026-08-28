"""Обёртки socket.io-хендлеров chainlit: журнал связи, loading и кнопка Stop.

chainlit на каждый connection_successful шлёт task_end, а «тихий» реконнект того
же сокета не идёт через on_chat_resume — индикатор хода гаснет, пока ход жив.
Обёртка возвращает task_start треду с живым ходом; connect, disconnect с причиной
engine.io и восстановление сессии пишутся в журнал.

Хендлер stop заменяется целиком: оригинал первым делом шлёт в ленту своё
«Task manually stopped.», а об остановке отчитывается сам ход — вторая
надпись в ленте лишняя.

Ошибки:
InternalServiceError — chainlit не зарегистрировал свои хендлеры сокета.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, cast

from boba.chainlit.infra.session import ChainlitSession, session_source_ref
from boba.chainlit.infra.thread_room import ChatRoomSurface, ThreadLive
from boba.identity.errors import InternalServiceError
from boba.identity.run import RunRegistry
from chainlit.config import config as chainlit_config
from chainlit.context import init_ws_context

__all__ = ["SocketEvents"]

logger = logging.getLogger(__name__)


class SocketEvent(StrEnum):
    """События socket.io, которые перехватывает или шлёт обёртка."""

    CONNECT = "connect"
    DISCONNECT = "disconnect"
    CONNECTED = "connection_successful"
    TASK_START = "task_start"
    STOP = "stop"


@dataclass(frozen=True)
class SocketFacts:
    """Состояние сокета в момент события: сессия, тред и живой ход."""

    sid: str
    session_id: str
    thread_id: str
    restored: bool
    turn_alive: bool

    @classmethod
    def of(cls, sid: str) -> SocketFacts:
        """Факты по сокету; сессии ещё (или уже) нет — поля пустые."""
        session = session_source_ref().of_socket(sid)
        if not session.present:
            return cls(
                sid=sid,
                session_id="",
                thread_id="",
                restored=False,
                turn_alive=False,
            )

        return cls.of_session(session)

    @classmethod
    def of_session(cls, session: ChainlitSession) -> SocketFacts:
        """Факты по известной сессии сокета."""
        thread_id = session.thread_id
        if not thread_id:
            thread_id = ""

        turn_alive = False
        if thread_id:
            turn_alive = RunRegistry.active(thread_id) is not None

        return cls(
            sid=session.socket_id,
            session_id=session.id,
            thread_id=thread_id,
            restored=session.restored,
            turn_alive=turn_alive,
        )

    def line(self) -> str:
        """Строка журнала: собирается здесь, а не в каждом месте вызова."""
        return (
            f"sid={self.sid} session={self.session_id} thread={self.thread_id} "
            f"restored={self.restored} turn_alive={self.turn_alive}"
        )


class SocketEvents:
    """Перехват хендлеров chainlit: журнал связи и loading при живом ходе."""

    NAMESPACE: ClassVar[str] = "/"

    _installed: ClassVar[bool] = False
    """Обёртки поверх обёрток вызвали бы оригиналы дважды."""

    @classmethod
    def install(cls) -> None:
        """Ставит обёртки; вызывать после импорта chainlit.socket."""
        from chainlit.server import sio  # noqa: PLC0415

        if cls._installed:
            logger.info("socket handlers already wrapped")
            return

        handlers = cast("dict[str, dict[str, Any]]", sio.handlers).get(cls.NAMESPACE)
        if not handlers:
            raise InternalServiceError(
                f"chainlit socket handlers are missing: namespace={cls.NAMESPACE}",
                None,
            )

        origin_connect = cls._origin(handlers, SocketEvent.CONNECT)
        origin_disconnect = cls._origin(handlers, SocketEvent.DISCONNECT)
        origin_connected = cls._origin(handlers, SocketEvent.CONNECTED)
        cls._origin(handlers, SocketEvent.STOP)

        async def connect(
            sid: str, environ: dict[str, Any], auth: dict[str, Any]
        ) -> Any:
            try:
                accepted = await origin_connect(sid, environ, auth)
            except ConnectionRefusedError as exc:
                # отказ на реконнекте выглядит как «сессия разорвана»
                logger.warning("socket refused: sid=%s reason=%s", sid, exc)
                raise

            facts = SocketFacts.of(sid)
            logger.info("socket connect: %s accepted=%s", facts.line(), accepted)

            return accepted

        async def disconnect(sid: str, reason: str = "") -> Any:
            facts = SocketFacts.of(sid)
            logger.info("socket disconnect: %s reason=%s", facts.line(), reason)

            return await origin_disconnect(sid)

        async def connected(sid: str) -> Any:
            result = await origin_connected(sid)
            await cls._restore_loading(sid)

            return result

        sio.on(SocketEvent.CONNECT.value, connect, namespace=cls.NAMESPACE)
        sio.on(SocketEvent.DISCONNECT.value, disconnect, namespace=cls.NAMESPACE)
        sio.on(SocketEvent.CONNECTED.value, connected, namespace=cls.NAMESPACE)
        sio.on(SocketEvent.STOP.value, cls._stop, namespace=cls.NAMESPACE)

        cls._installed = True

        logger.info(
            "socket handlers wrapped: %s, %s, %s, %s",
            SocketEvent.CONNECT.value,
            SocketEvent.DISCONNECT.value,
            SocketEvent.CONNECTED.value,
            SocketEvent.STOP.value,
        )

    @classmethod
    async def _stop(cls, sid: str) -> None:
        """Кнопка Stop: остановка хода, затем снятие задачи chainlit.

        Порядок обязателен: ход помечает причину остановки сам, и только после
        этого его задаче прилетает отмена — иначе ход прочитал бы её как обрыв
        снаружи и отчитался бы другой формулировкой.
        """
        session = session_source_ref().of_socket(sid)
        socket = session.websocket
        if socket is None:
            logger.info("socket stop without session: sid=%s", sid)
            return

        init_ws_context(socket)

        facts = SocketFacts.of_session(session)
        logger.info("socket stop: %s", facts.line())

        if chainlit_config.code.on_stop:
            await chainlit_config.code.on_stop()

        if socket.current_task:
            socket.current_task.cancel()

    @classmethod
    async def _restore_loading(cls, sid: str) -> None:
        """Возвращает индикатор хода: chainlit гасит его на каждом реконнекте."""
        session = session_source_ref().of_socket(sid)
        if not session.present:
            logger.warning("socket ready without session: sid=%s", sid)
            return

        facts = SocketFacts.of_session(session)
        socket = session.websocket
        if facts.thread_id and socket is not None:
            ChatRoomSurface.renderer_of(socket, facts.thread_id)

        alive = facts.turn_alive
        if not alive and facts.thread_id:
            alive = await ThreadLive.turn_alive(facts.thread_id)

        if not alive:
            logger.info("socket ready: %s", facts.line())
            return

        emit = cast(
            "Callable[[str, Any], Awaitable[None]]",
            session.emit,
        )
        await emit(SocketEvent.TASK_START.value, {})

        logger.info("socket ready, loading restored: %s", facts.line())

    @classmethod
    def _origin(
        cls, handlers: dict[str, Any], event: SocketEvent
    ) -> Callable[..., Awaitable[Any]]:
        """Хендлер chainlit до подмены; без него оборачивать нечего."""
        handler = handlers.get(event.value)
        if handler is None:
            raise InternalServiceError(
                f"chainlit socket handler is missing: event={event.value}",
                None,
            )

        return cast("Callable[..., Awaitable[Any]]", handler)
