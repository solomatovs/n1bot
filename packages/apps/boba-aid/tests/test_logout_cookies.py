"""Выход из чата: чужие имена cookie не ломают logout."""

import pytest
from starlette.requests import Request
from starlette.responses import Response

from boba.aid.infra.config import AppConfig

pytestmark = pytest.mark.anyio


class TestLogoutCookies:
    """Выход чистит только свою куку; чужие имена не валят обработчик."""

    async def test_foreign_cookie_names_do_not_break_logout(
        self, app_config: AppConfig
    ) -> None:
        from boba.aid.infra.callback import on_logout

        own = app_config.session.cookie
        cookies = f"Path=/; {own}=x; other=1".encode()
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/logout",
            "headers": [(b"cookie", cookies)],
            "query_string": b"",
        }
        request = Request(scope)
        response = Response()

        on_logout(request, response)

        cleared: list[str] = []
        for header in response.headers.getlist("set-cookie"):
            cleared.append(header.split("=", 1)[0])

        if cleared != [own]:
            raise AssertionError(
                f"logout clears only the session cookie {own!r}, got {cleared}"
            )
