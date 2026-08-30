"""Сигнал странице обновить вход по SPNEGO, отправляемый через шину.

Ошибки:
MessageBusError — шина не приняла сообщение; вызов инструмента получает её.
"""

from __future__ import annotations

from collections.abc import Callable

from boba.chainlit.domain.context import ChatCallContext
from boba.identity.context import CallContext
from boba.identity.sso import RefreshSignal
from boba.messaging import LockToken, MessageBus, SignInRefreshRequested

__all__ = ["BusRef", "ChatRefreshSignal"]

BusRef = Callable[[], MessageBus]


class ChatRefreshSignal(RefreshSignal):
    """Просит фронт молча пройти SPNEGO ещё раз: публикует SignInRefreshRequested в
    область треда текущего хода.
    """

    def __init__(self, bus: BusRef) -> None:
        self._bus = bus

    async def send(self) -> bool:
        context = CallContext.current()
        if not isinstance(context, ChatCallContext):
            return False

        message = SignInRefreshRequested(principal=context.subject.login)
        await self._bus().publish(context.scope, message, LockToken.local())

        return True
