"""Пользователь в каждой строке лога: подставляет фабрика LogRecord."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from boba.chainlit.infra import log_context
from boba.chainlit.infra.log_context import (
    RequestUserContext,
    RequestUserMiddleware,
    UserLogContext,
)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


@pytest.fixture(autouse=True)
def installed() -> None:
    UserLogContext.install()


class TestUserInEveryRecord:
    @staticmethod
    def _record() -> logging.LogRecord:
        return logging.getLogger("probe").makeRecord(
            "probe", logging.INFO, "f.py", 1, "сообщение", (), None
        )

    def test_attribute_present_without_session(self) -> None:
        record = self._record()
        assert getattr(record, UserLogContext.ATTRIBUTE) == UserLogContext.UNKNOWN

    def test_user_label_taken_from_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(log_context, "current_user_label", lambda: "ivanov")
        assert getattr(self._record(), UserLogContext.ATTRIBUTE) == "ivanov"

    def test_broken_session_does_not_break_logging(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom() -> str:
            msg = "нет контекста"
            raise RuntimeError(msg)

        monkeypatch.setattr(log_context, "current_user_label", boom)
        assert getattr(self._record(), UserLogContext.ATTRIBUTE) == (
            UserLogContext.UNKNOWN
        )

    def test_install_is_idempotent(self) -> None:
        factory = logging.getLogRecordFactory()
        UserLogContext.install()
        UserLogContext.install()
        assert logging.getLogRecordFactory() is factory

    def test_format_with_user_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(log_context, "current_user_label", lambda: "petrov")
        formatter = logging.Formatter("[%(user)s] %(message)s")
        assert formatter.format(self._record()) == "[petrov] сообщение"

    def test_request_context_used_without_session(self) -> None:
        token = RequestUserContext.set("sidorov")
        try:
            assert getattr(self._record(), UserLogContext.ATTRIBUTE) == "sidorov"
        finally:
            RequestUserContext.reset(token)

    def test_session_wins_over_request_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(log_context, "current_user_label", lambda: "ivanov")
        token = RequestUserContext.set("sidorov")
        try:
            assert getattr(self._record(), UserLogContext.ATTRIBUTE) == "ivanov"
        finally:
            RequestUserContext.reset(token)


class TestRequestUserMiddleware:
    class _JwtUser:
        identifier = "sidorov"

    @staticmethod
    def _scope(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
        return {"type": "http", "headers": headers, "path": "/", "query_string": b""}

    @staticmethod
    def _run(middleware: RequestUserMiddleware, scope: dict[str, Any]) -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request"}

        async def send(message: Any) -> None:
            pass

        asyncio.run(middleware(scope, receive, send))

    def test_label_visible_inside_request_and_reset_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import chainlit.auth.jwt

        monkeypatch.setattr(chainlit.auth.jwt, "decode_jwt", lambda _t: self._JwtUser())
        seen: list[str] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            seen.append(RequestUserContext.get())

        scope = self._scope([(b"authorization", b"Bearer whatever")])
        self._run(RequestUserMiddleware(app), scope)
        assert seen == ["sidorov"]
        assert RequestUserContext.get() == ""

    def test_broken_token_gives_empty_label(self) -> None:
        seen: list[str] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            seen.append(RequestUserContext.get())

        scope = self._scope([(b"cookie", b"access_token=garbage")])
        self._run(RequestUserMiddleware(app), scope)
        assert seen == [""]

    def test_non_http_scope_passthrough(self) -> None:
        called: list[bool] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            called.append(True)

        self._run(RequestUserMiddleware(app), {"type": "lifespan"})
        assert called == [True]
