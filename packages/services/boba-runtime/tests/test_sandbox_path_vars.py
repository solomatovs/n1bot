"""Контекст вызова на входе инструментов: путь workspace и чистка кук при выходе."""

from __future__ import annotations

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
from boba.runtime.launchers import CallSurface
from boba.sandbox import BindSpec
from boba.stand_core.context import CallStand

pytestmark = pytest.mark.anyio

WORKSPACE = "/app/boba/data/workspace/{user_id}.ext4:/workspace"
THREAD_ID = "e4baaef4-2887-455a-85ff-16d70dd3e4c9"


def _context(user_id: UUID) -> CallContext:
    return CallContext(
        subject=Subject(
            user_id=user_id, login="maksimov.ma", roles=frozenset(), profile="general"
        ),
        scope=Scope.chat(THREAD_ID),
        run_id=THREAD_ID,
        initiator=ChatInitiator(thread_id=THREAD_ID, turn_id="m1"),
        credential=NoUserCredential(reason="test"),
        cancellation=RunCancellation(),
    )


class TestSandboxPathVars:
    """Контекст вызова отдаёт значения, которыми профиль разворачивает пути."""

    async def test_context_fills_both_variables(self, call_stand: CallStand) -> None:
        with call_stand.contexts.applied(_context(UUID(int=18))):
            values = CallSurface(call_stand.contexts).sandbox_path_vars()

        if values != {"user_id": str(UUID(int=18)), "thread_id": THREAD_ID}:
            raise AssertionError(f"оба значения из контекста, дано {values!r}")

    async def test_workspace_path_renders_from_the_context(
        self, call_stand: CallStand
    ) -> None:
        """Путь образа собирается целиком: это и падало у bash."""
        with call_stand.contexts.applied(_context(UUID(int=18))):
            rendered = BindSpec.parse(WORKSPACE).render(
                CallSurface(call_stand.contexts).sandbox_path_vars()
            )

        if rendered.host != f"/app/boba/data/workspace/{UUID(int=18)}.ext4":
            raise AssertionError(f"host подставлен, дано {rendered.host!r}")

        if rendered.target != "/workspace":
            raise AssertionError(f"target не трогаем, дано {rendered.target!r}")

    async def test_each_user_gets_its_own_image(self, call_stand: CallStand) -> None:
        hosts: list[str] = []
        for user_id in (UUID(int=18), UUID(int=42)):
            with call_stand.contexts.applied(_context(user_id)):
                hosts.append(
                    BindSpec.parse(WORKSPACE)
                    .render(CallSurface(call_stand.contexts).sandbox_path_vars())
                    .host
                )

        if hosts != [
            f"/app/boba/data/workspace/{UUID(int=18)}.ext4",
            f"/app/boba/data/workspace/{UUID(int=42)}.ext4",
        ]:
            raise AssertionError(f"образ на пользователя, дано {hosts!r}")

    async def test_without_context_there_are_no_values(
        self, call_stand: CallStand
    ) -> None:
        """Вне контекста значений нет: профиль с переменными откажет рендером."""
        if CallSurface(call_stand.contexts).sandbox_path_vars() != {}:
            raise AssertionError("вне контекста переменных быть не должно")

        with pytest.raises(RuntimeError, match="user_id"):
            BindSpec.parse(WORKSPACE).render(
                CallSurface(call_stand.contexts).sandbox_path_vars()
            )
