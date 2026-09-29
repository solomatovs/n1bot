"""StaleSessionMiddleware: с cookie чужого поколения сессий запрос идёт к
приложению гостем, а ответ снимает cookie в браузере; своё поколение и прочие
негодные cookie проходят к маршрутам как есть."""

from __future__ import annotations

import asyncio
from typing import Any

from boba.auth import JwtTokens
from boba.identity.session import Login
from boba.identity.signin import SignedIn, SignInMetadata
from boba.identity.token import CookieSpec
from boba.runtime.http import StaleSessionMiddleware

SECRET = "stale-session-secret"
COOKIE = "access_token"


OTHER = "theme=dark"


class Outcome:
    """Что увидели приложение и клиент: заголовок Cookie, дошедший до
    приложения, статус и заголовки ответа."""

    def __init__(self) -> None:
        self.reached_app = False
        self.app_cookie = b""
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []


def _run(generation: str, cookie_value: str | None) -> Outcome:
    outcome = Outcome()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        outcome.reached_app = True
        for name, value in scope["headers"]:
            if name == b"cookie":
                outcome.app_cookie = value

        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    tokens = JwtTokens(SECRET, 60, generation)
    spec = CookieSpec(name=COOKIE, samesite="lax", ttl_sec=60)
    middleware = StaleSessionMiddleware(app, tokens=tokens, cookie=spec)

    headers: list[tuple[bytes, bytes]] = []
    if cookie_value is not None:
        headers.append((b"cookie", f"{OTHER}; {COOKIE}={cookie_value}".encode()))

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/user",
        "headers": headers,
        "query_string": b"",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(message: Any) -> None:
        if message["type"] == "http.response.start":
            outcome.status = message["status"]
            outcome.headers = list(message.get("headers", []))

    asyncio.run(middleware(scope, receive, send))

    return outcome


def _token(generation: str) -> str:
    signed = SignedIn(
        identifier=Login("alice"), display_name="Alice", sign_in=SignInMetadata()
    )

    return JwtTokens(SECRET, 60, generation).issue(signed)


def test_current_generation_reaches_the_app() -> None:
    outcome = _run("gen-a", _token("gen-a"))

    assert outcome.reached_app
    assert f"{COOKIE}=".encode() in outcome.app_cookie
    assert not [v for k, v in outcome.headers if k == b"set-cookie"]


def test_previous_generation_goes_on_as_a_guest() -> None:
    """Первая загрузка страницы после рестарта: вход рисуется, а не 401."""
    outcome = _run("gen-b", _token("gen-a"))

    assert outcome.reached_app
    assert outcome.status == 200
    assert outcome.app_cookie == OTHER.encode()


def test_previous_generation_cookie_is_cleared_by_the_response() -> None:
    outcome = _run("gen-b", _token("gen-a"))

    cleared = [v for k, v in outcome.headers if k == b"set-cookie"]
    assert any(v.startswith(f"{COOKIE}=".encode()) for v in cleared), cleared


def test_garbage_cookie_is_left_to_the_routes() -> None:
    outcome = _run("gen-a", "not-a-jwt")

    assert outcome.reached_app


def test_no_cookie_reaches_the_app() -> None:
    outcome = _run("gen-a", None)

    assert outcome.reached_app
