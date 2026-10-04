"""Сигнал обновления билета доходит до каждой вкладки пользователя на инстансе."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from chainlit_stand import StandTokens

from boba.chainlit.infra.session import ChainlitSession, ChainlitSessions
from boba.chainlit.infra.thread_room import ChatRoomSurface, UserRoom
from boba.chainlit.rendering.renderer import SignalType
from boba.identity.context import Scope
from boba.messaging import Envelope, SignInRefreshRequested

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Сессии подставляются напрямую: контекст chainlit тесту не нужен."""


class RecordingSession(ChainlitSession):
    """Вкладка пользователя, которая запоминает присланные ей события."""

    def __init__(self) -> None:
        super().__init__(None, StandTokens())
        self.events: list[tuple[str, Mapping[str, Any]]] = []

    async def emit(self, event: str, payload: Mapping[str, Any]) -> bool:
        self.events.append((event, dict(payload)))
        return True


async def test_refresh_reaches_every_tab_of_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = UUID(int=9)
    tabs = [RecordingSession(), RecordingSession()]
    monkeypatch.setattr(ChainlitSessions, "of_user", lambda self, uid: list(tabs))

    envelope = Envelope(
        scope=Scope.user(user_id),
        seq=1,
        at=datetime.now(UTC),
        origin="test",
        message=SignInRefreshRequested(principal="reader@EXAMPLE"),
    )

    await UserRoom.deliver(user_id, envelope)

    expected = (ChatRoomSurface.EVENT, {"type": SignalType.SIGNIN_REFRESH})
    for tab in tabs:
        if tab.events != [expected]:
            raise AssertionError(f"tab must get the refresh signal: {tab.events}")
