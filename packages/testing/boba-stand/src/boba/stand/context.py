"""Контекст вызова в тестах: личность без сессии приложения на время теста."""

from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from uuid import UUID

import pytest

from boba.cancellation import RunCancellation
from boba.identity.context import (
    CallContext,
    ChatInitiator,
    NoUserCredential,
    Scope,
    Subject,
)

TEST_TURN = "test-turn"
"""Метка хода в контекстах вызова, которые ставят тесты."""

TEST_PROFILE = "test"
TEST_USER_ID = UUID(int=7)
"""Пользователь тестового контекста по умолчанию."""
"""Профиль контекста вызова, если тест не назвал свой."""


def install_context(monkeypatch: pytest.MonkeyPatch, context: CallContext) -> None:
    """Ставит контекст вызова на время теста в любом контексте исполнения.

    Async-тесты живут в одном контексте раннера anyio, и set() на contextvar
    пережил бы тест; подмена самой переменной снимается monkeypatch'ем.
    """
    current: ContextVar[CallContext | None] = ContextVar(
        "boba_call_context", default=context
    )
    monkeypatch.setattr(CallContext, "_CURRENT", current)


def make_context(  # noqa: PLR0913 — личность собирается по частям, как в сессии
    thread_id: str,
    cancellation: RunCancellation | None = None,
    *,
    user_id: UUID = TEST_USER_ID,
    login: str = "tester",
    roles: Iterable[str] = (),
    profile: str = TEST_PROFILE,
) -> CallContext:
    """Контекст хода чата, как его собирает on_message, без сессии приложения."""
    if cancellation is None:
        cancellation = RunCancellation()

    return CallContext(
        subject=Subject(
            user_id=user_id, login=login, roles=frozenset(roles), profile=profile
        ),
        scope=Scope.chat(thread_id),
        initiator=ChatInitiator(thread_id=thread_id, turn_id=TEST_TURN),
        credential=NoUserCredential(reason="the test context carries no ticket"),
        cancellation=cancellation,
    )


def use_context(  # noqa: PLR0913 — личность собирается по частям, как в сессии
    monkeypatch: pytest.MonkeyPatch,
    *,
    thread_id: str,
    user_id: UUID = TEST_USER_ID,
    login: str = "tester",
    roles: Iterable[str] = (),
    profile: str = TEST_PROFILE,
) -> CallContext:
    """Ставит контекст вызова хода чата на время теста."""
    context = make_context(
        thread_id, user_id=user_id, login=login, roles=roles, profile=profile
    )
    install_context(monkeypatch, context)
    return context


@pytest.fixture(autouse=True)
def call_context_cleared() -> Iterator[None]:
    """Контекст вызова — contextvar: без сброса он утёк бы между sync-тестами."""
    CallContext.reset()
    yield
    CallContext.reset()
