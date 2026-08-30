"""Выход из чата: чужие имена cookie не ломают logout."""

import pytest
from starlette.requests import Request
from starlette.responses import Response

pytestmark = pytest.mark.anyio


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
