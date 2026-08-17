"""Данные текущей сессии chainlit: пользователь, тред."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final

import chainlit as cl
from boba.chainlit.domain.errors import RefusalError
from chainlit.context import ChainlitContextException

__all__ = [
    "LogLine",
    "LogUserMark",
    "UserMetadataField",
    "current_thread_id",
    "current_user_id",
    "current_user_label",
    "current_user_roles",
]


class UserMetadataField:
    "Ключи metadata у cl.User; контракт chainlit."

    PROVIDER: Final = "provider"
    ROLES: Final = "roles"


class LogLine:
    """Текст чужого происхождения в строке журнала: одна строка без управляющих.

    Ошибка инструмента и ответ модели попадают в лог как есть, а перевод строки
    внутри них подделал бы соседнюю запись журнала.
    """

    ESCAPES: ClassVar[Mapping[str, str]] = {
        "\\": "\\\\",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    DELETE: ClassVar[str] = "\x7f"

    @classmethod
    def safe(cls, text: str) -> str:
        chunks: list[str] = []

        for char in text:
            escape = cls.ESCAPES.get(char)
            if escape is not None:
                chunks.append(escape)
                continue

            if char < " " or char == cls.DELETE:
                chunks.append(f"\\x{ord(char):02x}")
                continue

            chunks.append(char)

        return "".join(chunks)


class LogUserMark:
    """Явная метка пользователя для строк лога вне контекста сессии chainlit.

    Колбэки инструментов langchain гоняет в чужом event loop'е, где сессии уже
    нет: метка ставится на время самой записи, а не наследуется из контекста.
    """

    THREAD_LEN: ClassVar[int] = 8

    _current: ClassVar[ContextVar[str]] = ContextVar("log_user_mark", default="")

    def __init__(self, user: str, thread_id: str) -> None:
        self._label = self.compose(user, thread_id)

    @classmethod
    def compose(cls, user: str, thread_id: str) -> str:
        """Метка строки лога: логин и короткий thread-id."""
        if not user:
            return ""

        if not thread_id:
            return user

        return f"{user} {thread_id[: cls.THREAD_LEN]}"

    @classmethod
    def current(cls) -> str:
        """Метка, выставленная на время записи; пустая — метки нет."""
        return cls._current.get()

    @contextlib.contextmanager
    def applied(self) -> Iterator[None]:
        token = self._current.set(self._label)
        try:
            yield
        finally:
            self._current.reset(token)


def _current_user() -> cl.User | cl.PersistedUser | None:
    try:
        return cl.user_session.get("user")
    except ChainlitContextException:
        return None


def current_user_roles() -> frozenset[str]:
    user = _current_user()
    if user is None:
        return frozenset()

    metadata = user.metadata
    if metadata is None:
        metadata = {}

    roles = metadata.get(UserMetadataField.ROLES)
    if not roles:
        roles = []
    if isinstance(roles, str):
        return frozenset({roles})

    return frozenset(str(r) for r in roles)


def current_user_id() -> str | None:
    user = _current_user()
    return getattr(user, "id", None)


def current_user_label() -> str:
    """Логин для логов; пустая строка — запрос идёт вне сессии chainlit."""
    user = _current_user()
    if user is None:
        return ""
    identifier = getattr(user, "identifier", "")
    if identifier:
        return str(identifier)
    return str(getattr(user, "id", ""))


def current_thread_id() -> str | None:
    try:
        return cl.context.session.thread_id
    except ChainlitContextException:
        return None

class SessionKind(StrEnum):
    """Отказы требований сессии: операции нужны пользователь и тред."""

    NO_SESSION = "no_session"
    NO_THREAD = "no_thread"


@dataclass(frozen=True)
class RequiredSession:
    """Пользователь и тред текущей сессии; без них операция отказывает.

    Ошибки: RefusalError с kind из SessionKind.
    """

    user_id: str
    thread_id: str

    @classmethod
    def of(cls) -> RequiredSession:
        user_id = current_user_id()
        if not user_id:
            raise RefusalError(SessionKind.NO_SESSION, "no chainlit user session")

        thread_id = current_thread_id()
        if not thread_id:
            raise RefusalError(SessionKind.NO_THREAD, "no active thread")

        return cls(user_id=str(user_id), thread_id=thread_id)
