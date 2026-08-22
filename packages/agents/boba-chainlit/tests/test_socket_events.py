"""Реконнект сокета при живом ходе: индикатор обязан вернуться.

chainlit на каждый connection_successful шлёт task_end, а «тихий» реконнект
не идёт через on_chat_resume — кнопка Stop пропадала при работающем
инструменте. Проверяются настоящие хендлеры chainlit под нашими обёртками и
настоящий реестр websocket-сессий.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest
from chainlit.config import config as chainlit_config
from chainlit.server import sio
from chainlit.session import WebsocketSession

from boba.chainlit.domain.turn import TurnContext, TurnPort
from boba.chainlit.infra.socket_events import SocketEvent, SocketEvents

pytestmark = pytest.mark.anyio

THREAD = "thread-socket-1"
SESSION_ID = "session-socket-1"
SOCKET_ID = "socket-socket-1"


class FakeTurn(TurnPort):
    """Ход под тест: реестру достаточно порта с шагом ответа."""

    answer_step_id = "answer-step"


class EmittedEvents:
    """Что ушло в сокет сессии: порядок событий важен так же, как состав.

    chainlit объявляет emit синхронным, а подставляет корутину sio.emit —
    запись синхронная, ожидаемое значение отдаётся отдельной корутиной.
    """

    def __init__(self) -> None:
        self.names: list[str] = []

    def emit(self, event: str, data: Any) -> Any:
        del data
        self.names.append(event)

        return self._sent()

    async def _sent(self) -> None:
        return None


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    "обёртки работают с сессией напрямую, http-контекст им не нужен"


@pytest.fixture(autouse=True)
def clean_contexts() -> None:
    "чистый реестр ходов на тест"
    TurnContext.reset()


@pytest.fixture(autouse=True)
def wrapped_handlers() -> None:
    "обёртки ставятся один раз на процесс, как в bootstrap"
    SocketEvents.install()


@pytest.fixture
async def session() -> AsyncIterator[EmittedEvents]:
    """Живая сессия треда в реестре chainlit; убирается за собой."""
    events = EmittedEvents()

    async def emit_call(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    session = WebsocketSession(
        id=SESSION_ID,
        socket_id=SOCKET_ID,
        emit=events.emit,
        emit_call=emit_call,
        user_env={},
        client_type="webapp",
        thread_id=THREAD,
    )
    # ветки chainlit вокруг on_chat_start в тесте не нужны: проверяется реконнект
    session.restored = True
    session.has_first_interaction = True
    session.chat_started = True

    yield events

    await session.delete()

    if WebsocketSession.get_by_id(SESSION_ID) is not None:
        raise AssertionError("сессия теста осталась в реестре chainlit")


async def _handler(event: SocketEvent) -> Any:
    handlers: dict[str, Any] = sio.handlers[SocketEvents.NAMESPACE]

    return handlers[event.value]


class TestLoadingSurvivesReconnect:
    """Ход живёт дольше сокета — индикатор хода обязан жить столько же."""

    async def test_live_turn_gets_task_start_back(self, session: EmittedEvents) -> None:
        """Реконнект при живом ходе: за task_end приходит task_start."""
        connected = await _handler(SocketEvent.CONNECTED)

        with TurnContext.open(THREAD, FakeTurn()):
            await connected(SOCKET_ID)

        if SocketEvent.TASK_START.value not in session.names:
            raise AssertionError(f"task_start не пришёл: {session.names}")

        started = session.names.index(SocketEvent.TASK_START.value)
        ended = session.names.index("task_end")
        if started < ended:
            raise AssertionError(f"task_start пришёл раньше task_end: {session.names}")

    async def test_idle_thread_stays_without_loading(
        self, session: EmittedEvents
    ) -> None:
        """Хода нет — индикатор не зажигается, лента остаётся спокойной."""
        connected = await _handler(SocketEvent.CONNECTED)

        await connected(SOCKET_ID)

        if SocketEvent.TASK_START.value in session.names:
            raise AssertionError(f"task_start у пустого треда: {session.names}")


class TestStopHandler:
    """Кнопка Stop: ход останавливается, служебного сообщения chainlit нет."""

    MESSAGE_EVENT: ClassVar[str] = "new_message"
    """Событие, которым chainlit шлёт в ленту своё «Task manually stopped.»."""

    async def test_stop_does_not_send_its_own_message(
        self, session: EmittedEvents, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stop = await _handler(SocketEvent.STOP)
        monkeypatch.setattr(chainlit_config.code, "on_stop", None, raising=False)

        with TurnContext.open(THREAD, FakeTurn()):
            await stop(SOCKET_ID)

        if self.MESSAGE_EVENT in session.names:
            raise AssertionError(f"в ленту ушло сообщение chainlit: {session.names}")

    async def test_turn_is_stopped_before_the_task_is_cancelled(
        self, session: EmittedEvents, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Причину остановки ставит ход, поэтому on_stop идёт первым."""
        del session
        stop = await _handler(SocketEvent.STOP)
        order: list[str] = []

        async def on_stop() -> None:
            order.append("on_stop")

        async def running() -> None:
            await asyncio.sleep(60)

        monkeypatch.setattr(chainlit_config.code, "on_stop", on_stop, raising=False)

        task = asyncio.create_task(running())
        task.add_done_callback(lambda _done: order.append("cancelled"))

        current = WebsocketSession.get(SOCKET_ID)
        if current is None:
            raise AssertionError("сессия теста жива")

        current.current_task = task

        await stop(SOCKET_ID)

        with pytest.raises(asyncio.CancelledError):
            await task

        if order != ["on_stop", "cancelled"]:
            raise AssertionError(f"порядок остановки: {order}")


class TestConnectionJournal:
    """Причину разрыва задаёт engine.io — без неё диагностика слепа."""

    async def test_disconnect_reason_reaches_the_log(
        self, session: EmittedEvents, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Разрыв пишется в журнал вместе с причиной и состоянием хода."""
        del session
        disconnect = await _handler(SocketEvent.DISCONNECT)
        reason = "ping timeout"

        journal = caplog.at_level(
            logging.INFO, logger="boba.chainlit.infra.socket_events"
        )
        with journal, TurnContext.open(THREAD, FakeTurn()):
            await disconnect(SOCKET_ID, reason)

        written = "\n".join(record.getMessage() for record in caplog.records)
        if reason not in written:
            raise AssertionError(f"причина разрыва не попала в журнал: {written}")

        if "turn_alive=True" not in written:
            raise AssertionError(f"живой ход не отмечен в журнале: {written}")
