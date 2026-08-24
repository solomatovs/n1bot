"""Сессия на входе инструментов: путь workspace и чистка кук при выходе."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from chainlit.context import init_http_context
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from starlette.requests import Request
from starlette.responses import Response

from boba.chainlit.infra.callback import on_logout
from boba.chainlit.infra.plugins import _sandbox_path_vars
from boba.sandbox import BindSpec

pytestmark = pytest.mark.anyio

WORKSPACE = "/app/boba/data/workspace/{user_id}.ext4:/workspace"
THREAD_ID = "e4baaef4-2887-455a-85ff-16d70dd3e4c9"


def _persisted(user_id: str) -> PersistedUser:
    stamp = datetime.now(tz=UTC).isoformat()

    return PersistedUser(
        id=user_id,
        identifier="maksimov.ma",
        createdAt=stamp,
        display_name="Maksimov.MA",
        metadata={},
    )


class TestSandboxPathVars:
    """Сессия отдаёт значения, которыми профиль разворачивает свои пути."""

    async def test_persisted_user_fills_both_variables(self) -> None:
        init_http_context(user=_persisted("18"), thread_id=THREAD_ID)

        values = _sandbox_path_vars()

        if values != {"user_id": "18", "thread_id": THREAD_ID}:
            raise AssertionError(f"оба значения из сессии, дано {values!r}")

    async def test_workspace_path_renders_from_the_session(self) -> None:
        """Путь образа собирается целиком: это и падало у bash."""
        init_http_context(user=_persisted("18"), thread_id=THREAD_ID)

        rendered = BindSpec.parse(WORKSPACE).render(_sandbox_path_vars())

        if rendered.host != "/app/boba/data/workspace/18.ext4":
            raise AssertionError(f"host подставлен, дано {rendered.host!r}")

        if rendered.target != "/workspace":
            raise AssertionError(f"target не трогаем, дано {rendered.target!r}")

    async def test_each_user_gets_its_own_image(self) -> None:
        init_http_context(user=_persisted("18"), thread_id=THREAD_ID)
        mine = BindSpec.parse(WORKSPACE).render(_sandbox_path_vars()).host

        init_http_context(user=_persisted("421"), thread_id=THREAD_ID)
        other = BindSpec.parse(WORKSPACE).render(_sandbox_path_vars()).host

        if mine == other:
            raise AssertionError("образ workspace обязан быть у каждого свой")

    async def test_user_without_id_is_named_in_the_refusal(self) -> None:
        """Вход, не сохранённый в базе, живёт как cl.User — у него нет id."""
        init_http_context(user=ChainlitUser(identifier="maksimov.ma"), thread_id="t-1")

        values = _sandbox_path_vars()

        if "user_id" in values:
            raise AssertionError(f"id взяться неоткуда, дано {values!r}")

        with pytest.raises(RuntimeError) as caught:
            BindSpec.parse(WORKSPACE).render(values)

        message = str(caught.value)
        if "no chainlit session" in message:
            raise AssertionError(f"сессия есть, отказ врёт: {message}")

        if "known: thread_id" not in message:
            raise AssertionError(f"отказ перечисляет, что есть: {message}")


class TestLogoutCookies:
    """Выход чистит только свои куки: домен общий с другими приложениями."""

    @staticmethod
    def _request(cookies: dict[str, str]) -> Request:
        header = "; ".join(f"{name}={value}" for name, value in cookies.items())
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/logout",
            "headers": [(b"cookie", header.encode())],
        }

        return Request(scope)

    @staticmethod
    def _deleted(response: Response) -> set[str]:
        names: set[str] = set()
        for key, value in response.raw_headers:
            if key.lower() != b"set-cookie":
                continue

            names.add(value.decode().split("=", 1)[0])

        return names

    def test_reserved_cookie_name_does_not_break_logout(self) -> None:
        """'Path' среди присланных кук http.cookies ставить не даёт."""
        request = self._request({"Path": "/", "access_token": "jwt", "grafana": "x"})
        response = Response()

        on_logout(request, response)

        deleted = self._deleted(response)
        if "Path" in deleted:
            raise AssertionError("зарезервированное имя трогать нельзя")

    def test_only_auth_cookies_are_cleared(self) -> None:
        request = self._request(
            {"access_token": "jwt", "access_token_0": "chunk", "grafana_session": "x"}
        )
        response = Response()

        on_logout(request, response)

        deleted = self._deleted(response)
        if "grafana_session" in deleted:
            raise AssertionError("куки соседних приложений выход не трогает")

        if not {"access_token", "access_token_0"} <= deleted:
            raise AssertionError(f"свои куки сняты, дано {deleted!r}")
