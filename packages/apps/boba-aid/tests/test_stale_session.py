"""StaleSessionMiddleware: с cookie чужого поколения сессий запрос идёт к
приложению гостем, а ответ снимает cookie в браузере; своё поколение и прочие
негодные cookie проходят к маршрутам как есть."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import jwt
import pytest
from aid_stand import StandTokens

from boba.aid.auth.cookies import CookieSpec
from boba.aid.auth.http import StaleSessionMiddleware
from boba.aid.auth.tokens import JwtTokens
from boba.identity.lifetime import SessionLifetime

SECRET = secrets.token_urlsafe(32)
COOKIE = "access_token"


OTHER = "theme=dark"


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """These pure-ASGI middleware tests do not create a Chainlit context."""


class Outcome:
    """Что увидели приложение и клиент: заголовок Cookie, дошедший до
    приложения, статус и заголовки ответа."""

    def __init__(self) -> None:
        self.reached_app = False
        self.app_cookie = b""
        self.app_authorization = b""
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []


def _run(
    generation: str,
    cookie_value: str | None,
    authorization: str = "",
    connection: str = "http",
) -> Outcome:
    outcome = Outcome()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        outcome.reached_app = True
        for name, value in scope["headers"]:
            if name == b"cookie":
                outcome.app_cookie = value
            if name == b"authorization":
                outcome.app_authorization = value

        if scope["type"] == "websocket":
            return

        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    tokens = JwtTokens(
        SECRET,
        SessionLifetime(ttl_sec=60, max_sec=3600),
        generation,
        issuer=StandTokens.ISSUER,
        audience=StandTokens.ISSUER,
    )
    spec = CookieSpec(name=COOKIE, samesite="lax", ttl_sec=60)
    middleware = StaleSessionMiddleware(app, tokens=tokens, cookie=spec)

    headers: list[tuple[bytes, bytes]] = []
    if cookie_value is not None:
        headers.append((b"cookie", f"{OTHER}; {COOKIE}={cookie_value}".encode()))

    if authorization:
        headers.append((b"authorization", authorization.encode()))

    scope: dict[str, Any] = {
        "type": connection,
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
    issued_at = int(time.time())
    user_id = uuid5(NAMESPACE_URL, "stale-session:corporate:alice")
    tokens = JwtTokens(
        SECRET,
        SessionLifetime(ttl_sec=60, max_sec=3600),
        generation,
        issuer=StandTokens.ISSUER,
        audience=StandTokens.ISSUER,
    )
    session = StandTokens.session("corporate:alice", user_id)
    return tokens.issue(session, issued_at)


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


def test_garbage_cookie_reaches_the_app_as_a_guest() -> None:
    outcome = _run("gen-a", "not-a-jwt")

    assert outcome.reached_app
    assert outcome.app_cookie == OTHER.encode()


def test_no_cookie_reaches_the_app() -> None:
    outcome = _run("gen-a", None)

    assert outcome.reached_app


@pytest.mark.parametrize("identifier", ["alice", "corporate:alice"])
@pytest.mark.parametrize("connection", ["http", "websocket"])
def test_legacy_identity_is_dropped(identifier: str, connection: str) -> None:
    token = jwt.encode(
        {
            "identifier": identifier,
            "metadata": {"generation": "gen-a"},
            "exp": 4102444800,
        },
        SECRET,
        algorithm="HS256",
    )
    outcome = _run("gen-a", token, connection=connection)
    assert outcome.reached_app
    assert outcome.app_cookie == OTHER.encode()


def test_lowercase_bearer_cannot_bypass_identity_check() -> None:
    token = jwt.encode(
        {
            "identifier": "corporate:alice",
            "metadata": {"generation": "gen-a"},
            "exp": 4102444800,
        },
        SECRET,
        algorithm="HS256",
    )
    outcome = _run("gen-a", None, authorization=f"bearer {token}")
    assert outcome.reached_app
    assert outcome.app_authorization == b""


def test_websocket_keeps_current_identity() -> None:
    outcome = _run("gen-a", _token("gen-a"), connection="websocket")
    assert f"{COOKIE}=".encode() in outcome.app_cookie


@pytest.mark.parametrize("connection", ["http", "websocket"])
def test_invalid_claims_cannot_bypass_identity_validation(connection: str) -> None:
    token = jwt.encode(
        {
            "identifier": "corporate:alice",
            "metadata": {"generation": "gen-a"},
            "exp": 4102444800,
            "since": "invalid",
        },
        SECRET,
        algorithm="HS256",
    )
    outcome = _run("gen-a", token, connection=connection)

    assert outcome.app_cookie == OTHER.encode()
