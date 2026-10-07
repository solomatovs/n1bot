"""Контекст вызова в тестах: личность без сессии приложения на время теста."""

from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, ClassVar
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
from boba.identity.errors import RefusalError
from boba.identity.run import ElementTarget, RunPort, RunRefusal
from boba.toolkit.result import ToolResultBase


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


class FakeTurn(RunPort):
    """Реализация RunPort для тестов: владелец запуска, который запоминает
    шаги вызовов и показанные элементы и адресует элемент вызова. Реестру
    запусков его достаточно, чтобы запуск считался ходом с лентой."""

    ANSWER_STEP: ClassVar[str] = "answer-step"

    def __init__(self) -> None:
        self.shown: list[tuple[str, Mapping[str, Any]]] = []
        self.started: list[tuple[str, str, Mapping[str, Any]]] = []
        self.media: dict[str, Mapping[str, str] | None] = {}
        """Начатые вызовы: идентификатор, имя инструмента, показанные аргументы."""
        self.finished: list[tuple[str, ToolResultBase]] = []
        self.failed: list[tuple[str, str]] = []
        self.progressed: list[tuple[str, float, float | None, str]] = []

    async def tool_started(
        self,
        tool_call_id: str,
        name: str,
        args: Mapping[str, Any],
        media: Mapping[str, str] | None,
    ) -> None:
        self.started.append((tool_call_id, name, dict(args)))
        self.media[tool_call_id] = media

    async def tool_progressed(
        self, tool_call_id: str, done: float, total: float | None, text: str
    ) -> None:
        self.progressed.append((tool_call_id, done, total, text))

    async def tool_finished(self, tool_call_id: str, result: ToolResultBase) -> None:
        self.finished.append((tool_call_id, result))

    async def tool_failed(self, tool_call_id: str, error: str) -> None:
        self.failed.append((tool_call_id, error))

    async def show_element(self, tool_call_id: str, element: Mapping[str, Any]) -> None:
        self.shown.append((tool_call_id, dict(element)))

    def element_target(self, tool_call_id: str) -> ElementTarget:
        if not tool_call_id:
            msg = f"stand element target needs a tool call id, got {tool_call_id!r}"
            raise RefusalError(RunRefusal.NO_TOOL_CALL, msg)

        return ElementTarget(
            for_id=self.ANSWER_STEP, element_id=f"element-{tool_call_id}"
        )


@pytest.fixture
def call_stand() -> CallStand:
    """Контекст вызова теста: свой держатель на каждый тест."""
    return CallStand()
