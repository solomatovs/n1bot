"""Сторож сессий: сигнал обновления уходит тем, чей токен на исходе, по одному на
пользователя; истёкшие и свежие токены сигнала не получают."""

from __future__ import annotations

import time
from uuid import UUID

import jwt
import pytest

from boba.auth import JwtTokens
from boba.identity.context import Scope
from boba.identity.token import SessionRenewal
from boba.messaging import Envelope, MemoryMessageBus, SignInRefreshRequested
from boba.runtime.refresh import LiveSessions, LiveToken, SessionKeeper

pytestmark = pytest.mark.anyio

SECRET = "keeper-secret"


class StandSessions(LiveSessions):
    """Живые сессии стенда: список задаёт тест."""

    def __init__(self, tokens: list[LiveToken]) -> None:
        self._tokens = tokens

    def live_tokens(self) -> list[LiveToken]:
        return list(self._tokens)


def _token(identifier: str, expires_in: int) -> str:
    now = int(time.time())
    claims = {
        "identifier": identifier,
        "metadata": {},
        "exp": now + expires_in,
        "iat": now,
        "since": now,
    }
    # nosemgrep: jwt-python-hardcoded-secret — секрет теста
    return jwt.encode(claims, SECRET, algorithm="HS256")


async def test_sweep_signals_each_expiring_user_once() -> None:
    bus = MemoryMessageBus("keeper-test")
    soon = UUID(int=1)
    fresh = UUID(int=2)
    dead = UUID(int=3)
    sessions = StandSessions(
        [
            LiveToken(user_id=soon, login="soon", token=_token("soon", 100)),
            LiveToken(user_id=soon, login="soon", token=_token("soon", 120)),
            LiveToken(user_id=fresh, login="fresh", token=_token("fresh", 3000)),
            LiveToken(user_id=dead, login="dead", token=_token("dead", -100)),
        ]
    )
    seen: list[Envelope] = []

    async def listener(envelope: Envelope) -> None:
        seen.append(envelope)

    for user_id in (soon, fresh, dead):
        bus.subscribe(Scope.user(user_id), listener)

    keeper = SessionKeeper(
        bus,
        sessions,
        JwtTokens(SECRET, 3600),
        SessionRenewal.of(3600, 86400),
        period_sec=60,
    )

    signalled = await keeper.sweep()

    assert signalled == 1
    assert len(seen) == 1
    assert seen[0].scope == Scope.user(soon)
    message = seen[0].message
    assert isinstance(message, SignInRefreshRequested)
    assert message.principal == "soon"
