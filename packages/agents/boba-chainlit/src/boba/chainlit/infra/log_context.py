"""Пользователь сессии в каждой строке лога через фабрику LogRecord."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from typing import Any, ClassVar

from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from boba.chainlit.infra.session import current_thread_id, current_user_label

__all__ = ["RequestUserContext", "RequestUserMiddleware", "UserLogContext"]


class RequestUserContext:
    """Логин пользователя текущего HTTP/WS-запроса; вне chainlit-сессии."""

    _label: ClassVar[ContextVar[str]] = ContextVar("request_user_label", default="")

    @classmethod
    def set(cls, label: str) -> Token[str]:
        return cls._label.set(label)

    @classmethod
    def reset(cls, token: Token[str]) -> None:
        cls._label.reset(token)

    @classmethod
    def get(cls) -> str:
        return cls._label.get()


class RequestUserMiddleware:
    """ASGI: кладёт логин из auth-токена chainlit в RequestUserContext.

    Access-лог uvicorn пишется внутри запроса, поэтому видит contextvar.
    """

    SCOPE_TYPES: ClassVar[frozenset[str]] = frozenset({"http", "websocket"})

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in self.SCOPE_TYPES:
            await self._app(scope, receive, send)
            return

        token = RequestUserContext.set(self._resolve(scope))
        try:
            await self._app(scope, receive, send)
        finally:
            RequestUserContext.reset(token)

    @staticmethod
    def _resolve(scope: Scope) -> str:
        # логирование best effort: битый/просроченный токен не валит запрос
        from chainlit.auth.cookie import get_token_from_cookies  # noqa: PLC0415
        from chainlit.auth.jwt import decode_jwt  # noqa: PLC0415

        try:
            connection = HTTPConnection(scope)
            raw = get_token_from_cookies(connection.cookies)
            if not raw:
                authorization = connection.headers.get("Authorization", "")
                scheme, _, param = authorization.partition(" ")
                if scheme.lower() == "bearer":
                    raw = param.strip()
            if not raw:
                return ""
            user = decode_jwt(raw)
            return str(user.identifier)
        except Exception:
            return ""


class UserLogContext:
    """Добавляет полю `user` логин и короткий thread-id из сессии chainlit."""

    ATTRIBUTE: ClassVar[str] = "user"
    UNKNOWN: ClassVar[str] = "-"
    THREAD_LEN: ClassVar[int] = 8

    _installed: ClassVar[bool] = False
    _previous: ClassVar[Any] = None

    @classmethod
    def install(cls) -> None:
        """Идемпотентно: повторный вызов не наслаивает фабрики."""
        if cls._installed:
            return
        cls._previous = logging.getLogRecordFactory()
        logging.setLogRecordFactory(cls._make_record)
        cls._installed = True

    @classmethod
    def _make_record(cls, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = cls._previous(*args, **kwargs)
        setattr(record, cls.ATTRIBUTE, cls._label())
        return record

    @classmethod
    def _label(cls) -> str:
        # логирование не имеет права падать из-за отсутствия контекста
        try:
            label = current_user_label()
        except Exception:
            label = ""
        if not label:
            label = RequestUserContext.get()
        if not label:
            return cls.UNKNOWN

        thread = cls._thread()
        if thread:
            return f"{label} {thread}"
        return label

    @classmethod
    def _thread(cls) -> str:
        try:
            thread_id = current_thread_id()
        except Exception:
            return ""
        if not thread_id:
            return ""
        return thread_id[: cls.THREAD_LEN]
