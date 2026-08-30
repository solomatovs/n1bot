"""Обновление входа без участия пользователя: сигнал странице по шине и сторож
сессий, который шлёт его тем, чей токен на исходе.

BusRefreshSignal публикует SignInRefreshRequested в область пользователя текущего
вызова; SessionKeeper раз в период обходит живые сессии инстанса (порт
LiveSessions — его реализуют приложения) и шлёт тот же сигнал по правилу
SessionRenewal. Страницы chainlit и studio слушают свою область и делают refresh.

Ошибки:
MessageBusError — шина не приняла сообщение; вызов инструмента получает её, сторож
    пишет в журнал и продолжает.
RefusalError — BusRefreshSignal вне контекста вызова: просить некого.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import abstractmethod
from collections.abc import Callable, Sequence
from typing import ClassVar, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from boba.identity.context import CallContext, Scope
from boba.identity.sso import RefreshSignal
from boba.identity.token import SessionRenewal, TokenReader, TokenRejectedError
from boba.messaging import LockToken, MessageBus, SignInRefreshRequested

__all__ = ["BusRefreshSignal", "LiveSessions", "LiveToken", "SessionKeeper"]

logger = logging.getLogger(__name__)


class BusRefreshSignal(RefreshSignal):
    """Просит фронт молча пройти обновление входа через шину процесса."""

    def __init__(self, bus: Callable[[], MessageBus]) -> None:
        self._bus = bus

    async def send(self) -> bool:
        subject = CallContext.current().subject
        message = SignInRefreshRequested(principal=subject.login)
        await self._bus().publish(
            Scope.user(subject.user_id), message, LockToken.local()
        )

        return True


class LiveToken(BaseModel):
    """Живая сессия инстанса: чья она и каким токеном входа держится."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    login: str = Field(min_length=1)
    token: str = Field(min_length=1)


class LiveSessions(Protocol):
    """Сессии, живые на этом инстансе сейчас: сокеты чата или страницы."""

    @abstractmethod
    def live_tokens(self) -> Sequence[LiveToken]: ...


class SessionKeeper:
    """Сторож сессий: пользователям с токеном на исходе шлёт сигнал обновления."""

    NAME: ClassVar[str] = "session-keeper"

    def __init__(
        self,
        bus: MessageBus,
        sessions: LiveSessions,
        tokens: TokenReader,
        renewal: SessionRenewal,
        period_sec: float,
    ) -> None:
        self._bus = bus
        self._sessions = sessions
        self._tokens = tokens
        self._renewal = renewal
        self._period_sec = period_sec
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=self.NAME)

    async def stop(self) -> None:
        if self._task is None:
            return

        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

        self._task = None

    async def sweep(self) -> int:
        """Один обход: сколько пользователей получили сигнал."""
        now = int(time.time())
        signalled: dict[UUID, str] = {}
        for live in self._sessions.live_tokens():
            if live.user_id in signalled:
                continue

            try:
                claims = self._tokens.read(live.token)
            except TokenRejectedError:
                # истёкший токен обновлять поздно: страница узнает об этом на запросе
                continue

            if self._renewal.should_refresh(claims, now):
                signalled[live.user_id] = live.login

        for user_id, login in signalled.items():
            message = SignInRefreshRequested(principal=login)
            await self._bus.publish(Scope.user(user_id), message, LockToken.local())
            logger.info("session keeper: refresh requested [user=%s]", login)

        return len(signalled)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._period_sec)
            try:
                await self.sweep()
            except Exception:
                # сторож переживает сбой одного обхода: следующий через период
                logger.exception("session keeper sweep failed")
