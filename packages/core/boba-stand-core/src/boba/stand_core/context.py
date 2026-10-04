"""Контекст вызова в тестах: личность без сессии приложения на время теста."""

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

import pytest

from boba.cancellation import RunCancellation
from boba.identity.context import (
    CallContext,
    CallContexts,
    ChatInitiator,
    NoUserCredential,
    Scope,
    Subject,
)


class StandIdentity(StrEnum):
    """Личность тестового контекста по умолчанию: метка хода, профиль, логин."""

    TURN = "test-turn"
    PROFILE = "test"
    LOGIN = "tester"


class StandContexts(CallContexts):
    """Держатель контекста вызова одного теста.

    Создаётся в CallStand и уходит коду под тестом как обычный CallContexts.
    Кроме контекста блока applied() знает закреплённый контекст теста: он
    виден из любого контекста исполнения — и sync-тесту, и задаче раннера
    anyio, и фикстуре, — чего одна переменная контекста исполнения не даёт.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pinned: CallContext | None = None

    def pin(self, context: CallContext | None) -> None:
        self._pinned = context

    def peek(self) -> CallContext | None:
        applied = super().peek()
        if applied is not None:
            return applied

        return self._pinned


class CallStand:
    """Контекст вызова теста: держатель и сборка контекстов хода чата.

    Создаётся фикстурой call_stand на каждый тест. Держатель contexts тест
    передаёт в конструкторы кода под тестом; контекст ставит use() — до конца
    теста — или applied() — на блок.
    """

    USER_ID: ClassVar[UUID] = UUID(int=7)

    def __init__(self) -> None:
        self._contexts = StandContexts()

    @property
    def contexts(self) -> CallContexts:
        return self._contexts

    def context(  # noqa: PLR0913 — личность собирается по частям, как в сессии
        self,
        thread_id: str,
        cancellation: RunCancellation | None = None,
        *,
        user_id: UUID = USER_ID,
        login: str = StandIdentity.LOGIN,
        roles: Iterable[str] = (),
        profile: str = StandIdentity.PROFILE,
    ) -> CallContext:
        """Контекст хода чата, как его собирает on_message, без сессии приложения."""
        if cancellation is None:
            cancellation = RunCancellation()

        return CallContext(
            subject=Subject(
                user_id=user_id, login=login, roles=frozenset(roles), profile=profile
            ),
            scope=Scope.chat(thread_id),
            run_id=thread_id,
            initiator=ChatInitiator(thread_id=thread_id, turn_id=StandIdentity.TURN),
            credential=NoUserCredential(reason="the test context carries no ticket"),
            cancellation=cancellation,
        )

    def use(self, context: CallContext) -> CallContext:
        """Ставит контекст до конца теста в любом контексте исполнения."""
        self._contexts.pin(context)
        return context

    def clear(self) -> None:
        """Снимает контекст, поставленный use()."""
        self._contexts.pin(None)

    @contextmanager
    def applied(self, context: CallContext) -> Generator[CallContext, None, None]:
        """Ставит контекст и метку лога на время блока."""
        with self._contexts.applied(context):
            yield context


@pytest.fixture
def call_stand() -> CallStand:
    """Контекст вызова теста: свой держатель на каждый тест."""
    return CallStand()
