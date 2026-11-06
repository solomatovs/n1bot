"""Сторож сессий: сигнал обновления уходит тем, чей токен на исходе, по одному на
пользователя; истёкшие и свежие токены сигнала не получают."""

from __future__ import annotations

import time
from uuid import UUID

import pytest
from aid_stand import StandTokens

from boba.aid.auth.policy import SessionRenewal
from boba.aid.auth.tokens import JwtTokens
from boba.aid.infra.refresh import LiveSessions, LiveToken, SessionKeeper
from boba.chat.messages import SignInRefreshRequested
from boba.identity.context import Scope
from boba.identity.lifetime import SessionLifetime
from boba.messaging import Envelope
from boba.stand.messaging import memory_message_bus

pytestmark = pytest.mark.anyio

SECRET = "keeper-secret"


class StandSessions(LiveSessions):
    """Живые сессии стенда: список задаёт тест."""

    def __init__(self, tokens: list[LiveToken]) -> None:
        self._tokens = tokens

    def live_tokens(self) -> list[LiveToken]:
        return list(self._tokens)


def _token(identifier: str, user_id: UUID, expires_in: int) -> str:
    now = int(time.time())
    lifetime = SessionLifetime(ttl_sec=3600, max_sec=86400)
    tokens = JwtTokens(
        SECRET,
        lifetime,
        "stand-generation",
        issuer=StandTokens.ISSUER,
        audience=StandTokens.ISSUER,
    )
    issued_at = now + expires_in - lifetime.ttl_sec
    session = StandTokens.session(identifier, user_id)
    return tokens.issue(session, issued_at)


async def test_sweep_signals_each_expiring_user_once() -> None:
    bus = memory_message_bus("keeper-test")
    soon = UUID(int=1)
    fresh = UUID(int=2)
    dead = UUID(int=3)
    sessions = StandSessions(
        [
            LiveToken(
                user_id=soon,
                login="soon",
                token=_token("soon", soon, 100),
            ),
            LiveToken(
                user_id=soon,
                login="soon",
                token=_token("soon", soon, 120),
            ),
            LiveToken(
                user_id=fresh,
                login="fresh",
                token=_token("fresh", fresh, 3000),
            ),
            LiveToken(
                user_id=dead,
                login="dead",
                token=_token("dead", dead, -100),
            ),
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
        JwtTokens(
            SECRET,
            SessionLifetime(ttl_sec=3600, max_sec=86400),
            "stand-generation",
            issuer=StandTokens.ISSUER,
            audience=StandTokens.ISSUER,
        ),
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
