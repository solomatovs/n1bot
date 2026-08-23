"""Ожидание повторного SSO-обмена: кто ждёт свежие делегированные креды входа.

Тикет входа живёт меньше сессии, а продлить constrained-креды нечем. Вместо
повторного логина приложение просит браузер молча пройти SPNEGO ещё раз:
ожидающая сторона висит здесь, приём кредов её будит.

Ожидание заводится до просьбы и снимается в `with`: иначе обмен, успевший
пройти раньше, разбудил бы пустое место, и вызов ждал бы весь таймаут.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import TracebackType
from typing import ClassVar, Self

__all__ = ["RefreshWaiters", "RefreshWaiting"]

logger = logging.getLogger(__name__)


class RefreshWaiting:
    """Одно ожидание метки входа: живёт от просьбы обменяться до её итога."""

    def __init__(self, waiters: RefreshWaiters, login: str, event: asyncio.Event):
        self._waiters = waiters
        self._login = login
        self._event = event

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self._waiters.release(self._login)

    async def wait(self, timeout: float) -> bool:
        """Ждёт свежих кредов входа; False — не дождались за timeout."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except TimeoutError:
            logger.info("kerberos: no refreshed credentials in %.1fs", timeout)
            return False

        return True


class RefreshWaiters:
    """Ожидания свежих кредов по метке входа: одно событие на метку."""

    TIMEOUT_SEC: ClassVar[float] = 10.0
    """Сколько ждать повторного обмена: браузер домена укладывается в секунду."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._holders: dict[str, int] = {}
        self._lock = threading.Lock()

    def arm(self, login: str) -> RefreshWaiting:
        """Заводит ожидание метки; звать до того, как просить об обмене."""
        with self._lock:
            event = self._events.get(login)
            if event is None:
                event = asyncio.Event()
                self._events[login] = event

            self._holders[login] = self._holders.get(login, 0) + 1

        return RefreshWaiting(self, login, event)

    def notify(self, login: str) -> None:
        """Будит ожидающих метку: креды входа только что обновились."""
        with self._lock:
            event = self._events.get(login)

        if event is None:
            return

        event.set()

    def release(self, login: str) -> None:
        """Снимает одно ожидание; последнее уносит с собой и событие."""
        with self._lock:
            holders = self._holders.get(login, 0) - 1
            if holders > 0:
                self._holders[login] = holders
                return

            self._holders.pop(login, None)
            self._events.pop(login, None)

    def forget(self, login: str) -> None:
        """Забывает ожидание входа: сессия закончилась."""
        with self._lock:
            self._holders.pop(login, None)
            self._events.pop(login, None)
