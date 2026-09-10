"""StaleSessionMiddleware: cookie чужого поколения сессий получает 401 и снимается,
своё поколение и прочие негодные cookie проходят к маршрутам."""

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


class Outcome:
    """Что увидел клиент: статус ответа middleware либо факт вызова приложения."""

    def __init__(self) -> None:
        self.reached_app = False
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []


def _run(generation: str, cookie_value: str | None) -> Outcome:
    outcome = Outcome()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        outcome.reached_app = True

    tokens = JwtTokens(SECRET, 60, generation)
    spec = CookieSpec(name=COOKIE, samesite="lax", ttl_sec=60)
    middleware = StaleSessionMiddleware(app, tokens=tokens, cookie=spec)

    headers: list[tuple[bytes, bytes]] = []
    if cookie_value is not None:
        headers.append((b"cookie", f"{COOKIE}={cookie_value}".encode()))

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


def test_previous_generation_is_401_and_the_cookie_is_cleared() -> None:
    outcome = _run("gen-b", _token("gen-a"))

    assert not outcome.reached_app
    assert outcome.status == 401
    cleared = [v for k, v in outcome.headers if k == b"set-cookie"]
    assert any(v.startswith(f"{COOKIE}=".encode()) for v in cleared)


def test_garbage_cookie_is_left_to_the_routes() -> None:
    outcome = _run("gen-a", "not-a-jwt")

    assert outcome.reached_app


def test_no_cookie_reaches_the_app() -> None:
    outcome = _run("gen-a", None)

    assert outcome.reached_app
