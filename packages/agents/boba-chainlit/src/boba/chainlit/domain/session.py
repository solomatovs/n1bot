"""Данные текущей сессии chainlit: пользователь, тред."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final

import chainlit as cl
from boba.chainlit.domain.errors import RefusalError
from chainlit.context import ChainlitContextException
from chainlit.session import WebsocketSession

__all__ = [
    "LogLine",
    "LogUserMark",
    "UserMetadataField",
    "current_chat_profile",
    "current_language",
    "current_thread_id",
    "current_user_id",
    "current_user_label",
    "current_user_metadata",
    "current_user_roles",
    "roles_of_user",
]


class UserMetadataField:
    "Ключи metadata у cl.User; контракт chainlit."

    PROVIDER: Final = "provider"
    ROLES: Final = "roles"
    LLM: Final = "llm"


class LogLine:
    """Текст чужого происхождения в строке журнала: одна строка без управляющих.

    Ошибка инструмента и ответ модели попадают в лог как есть, а перевод строки
    внутри них подделал бы соседнюю запись журнала. Экранирует json: он
    штатный кодировщик, а не своя таблица подстановок.
    """

    @classmethod
    def safe(cls, text: str) -> str:
        return json.dumps(text, ensure_ascii=False)


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


def roles_of_user(user: cl.User | cl.PersistedUser | None) -> frozenset[str]:
    """Роли пользователя из metadata; годится и вне контекста сессии."""
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


def current_user_roles() -> frozenset[str]:
    return roles_of_user(_current_user())


def current_user_metadata() -> dict[str, object]:
    """Metadata пользователя сессии; вне сессии — пустой словарь."""
    user = _current_user()
    if user is None:
        return {}

    metadata = user.metadata
    if metadata is None:
        return {}

    return dict(metadata)


def current_language() -> str:
    """Язык вкладки; вне ws-сессии — пустая строка."""
    try:
        session = cl.context.session
    except ChainlitContextException:
        return ""

    if not isinstance(session, WebsocketSession):
        return ""

    return session.language


def current_chat_profile() -> str | None:
    """Имя профиля чата текущей сессии; None — профиль не выбран."""
    try:
        value = cl.user_session.get("chat_profile")
    except ChainlitContextException:
        return None

    if not value:
        return None

    return str(value)


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
