"""Контекст вызова на входе инструментов: путь workspace и чистка кук при выходе."""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.responses import Response

from boba.cancellation import RunCancellation
from boba.chainlit.domain.context import (
    CallContext,
    ChatInitiator,
    NoUserCredential,
    Scope,
    Subject,
)
from boba.chainlit.infra.plugins import _sandbox_path_vars
from boba.sandbox import BindSpec

pytestmark = pytest.mark.anyio

WORKSPACE = "/app/boba/data/workspace/{user_id}.ext4:/workspace"
THREAD_ID = "e4baaef4-2887-455a-85ff-16d70dd3e4c9"


def _context(user_id: int) -> CallContext:
    return CallContext(
        subject=Subject(
            user_id=user_id, login="maksimov.ma", roles=frozenset(), profile="general"
        ),
        scope=Scope.chat(THREAD_ID),
        initiator=ChatInitiator(thread_id=THREAD_ID, turn_id="m1"),
        credential=NoUserCredential(reason="test"),
        cancellation=RunCancellation(),
    )


class TestSandboxPathVars:
    """Контекст вызова отдаёт значения, которыми профиль разворачивает пути."""

    async def test_context_fills_both_variables(self) -> None:
        with _context(18).applied():
            values = _sandbox_path_vars()

        if values != {"user_id": "18", "thread_id": THREAD_ID}:
            raise AssertionError(f"оба значения из контекста, дано {values!r}")

    async def test_workspace_path_renders_from_the_context(self) -> None:
        """Путь образа собирается целиком: это и падало у bash."""
        with _context(18).applied():
            rendered = BindSpec.parse(WORKSPACE).render(_sandbox_path_vars())

        if rendered.host != "/app/boba/data/workspace/18.ext4":
            raise AssertionError(f"host подставлен, дано {rendered.host!r}")

        if rendered.target != "/workspace":
            raise AssertionError(f"target не трогаем, дано {rendered.target!r}")

    async def test_each_user_gets_its_own_image(self) -> None:
        hosts: list[str] = []
        for user_id in (18, 42):
            with _context(user_id).applied():
                hosts.append(
                    BindSpec.parse(WORKSPACE).render(_sandbox_path_vars()).host
                )

        if hosts != [
            "/app/boba/data/workspace/18.ext4",
            "/app/boba/data/workspace/42.ext4",
        ]:
            raise AssertionError(f"образ на пользователя, дано {hosts!r}")

    async def test_without_context_there_are_no_values(self) -> None:
        """Вне контекста значений нет: профиль с переменными откажет рендером."""
        if _sandbox_path_vars() != {}:
            raise AssertionError("вне контекста переменных быть не должно")

        with pytest.raises(RuntimeError, match="user_id"):
            BindSpec.parse(WORKSPACE).render(_sandbox_path_vars())


class TestLogoutCookies:
    """Выход чистит только свою куку; чужие имена не валят обработчик."""

    async def test_foreign_cookie_names_do_not_break_logout(self) -> None:
        from boba.chainlit.infra.callback import on_logout

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/logout",
            "headers": [(b"cookie", b"Path=/; access_token=x; other=1")],
            "query_string": b"",
        }
        request = Request(scope)
        response = Response()

        on_logout(request, response)
