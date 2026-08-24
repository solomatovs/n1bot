"""Сессия чата в терминах домена: контракт, требования и метки входа.

Транспорта здесь нет: chainlit живёт в реализации (infra/session.py), а
логика знает только этот протокол. Так работа с сессией остаётся в одном
месте, не таща за собой веб-фреймворк.

Ошибки:
RefusalError — требование сессии не выполнено, kind из SessionKind.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final, Protocol

from boba.chainlit.domain.errors import RefusalError

__all__ = [
    "LogLine",
    "LogUserMark",
    "RequiredSession",
    "Session",
    "SessionKind",
    "SessionSource",
    "SsoMarks",
    "UserLogin",
    "UserMetadataField",
]


class UserMetadataField:
    "Ключи metadata у cl.User; контракт chainlit."

    PROVIDER: Final = "provider"
    PRINCIPAL: Final = "principal"
    LOGIN: Final = "sso_login"
    ROLES: Final = "roles"
    LLM: Final = "llm"


@dataclass(frozen=True)
class UserLogin:
    """Логин входа: ключ строки users и его человеческий вид.

    Регистр набора не заводит вторую личность: в identifier уходит key,
    а исходное написание остаётся именем в интерфейсе. Источник логина
    выбирает провайдер: ввод формы, sAMAccountName каталога, принципал.
    """

    key: str
    display: str

    @classmethod
    def of(cls, raw: str) -> UserLogin:
        name = raw.strip()

        return cls(key=name.lower(), display=name)


@dataclass(frozen=True)
class SsoMarks:
    """Метки SSO-входа в подписанном JWT: чей тикет и какому входу он выдан."""

    principal: str
    login: str

    @classmethod
    def of_metadata(cls, metadata: Mapping[str, object]) -> SsoMarks | None:
        """Метки из metadata пользователя; None — вход не нёс делегирования."""
        principal = metadata.get(UserMetadataField.PRINCIPAL)
        if not isinstance(principal, str) or not principal:
            return None

        login = metadata.get(UserMetadataField.LOGIN)
        if not isinstance(login, str) or not login:
            return None

        return cls(principal=principal, login=login)


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


class Session(Protocol):
    """Что доменная логика спрашивает у сессии чата.

    Реализация живёт в infra и знает про chainlit; здесь — только смысл:
    кто пришёл, в каком треде и с какими правами.
    """

    @property
    def present(self) -> bool:
        """Есть ли за обёрткой живая сессия."""
        ...

    @property
    def user_id(self) -> str | None:
        """id строки users; None — вход не сохранён слоем данных."""
        ...

    @property
    def thread_id(self) -> str | None: ...

    @property
    def identifier(self) -> str:
        """Логин, каким его записал вход; пустая строка — пользователя нет."""
        ...

    @property
    def label(self) -> str:
        """Имя для журнала; пустая строка — вызов идёт вне сессии."""
        ...

    @property
    def roles(self) -> frozenset[str]: ...

    @property
    def chat_profile(self) -> str | None: ...

    @property
    def metadata(self) -> Mapping[str, object]: ...

    def require(self) -> RequiredSession:
        """Пользователь и тред; без них операция отказывает."""
        ...


class SessionSource(Protocol):
    """Откуда берётся сессия текущего вызова."""

    def current(self) -> Session: ...


class SessionKind(StrEnum):
    """Отказы требований сессии: операции нужны пользователь и тред."""

    NO_SESSION = "no_session"
    NO_THREAD = "no_thread"


@dataclass(frozen=True)
class RequiredSession:
    """Пользователь и тред сессии; без них операция отказывает.

    Собирается ChatSession.require(): требование к сессии живёт там же,
    где и сама сессия.
    """

    user_id: str
    thread_id: str

    @classmethod
    def of(cls, session: Session) -> RequiredSession:
        """Требование к переданной сессии; отказ называет, чего не хватило."""
        user_id = session.user_id
        if not user_id:
            raise RefusalError(SessionKind.NO_SESSION, "no chainlit user session")

        thread_id = session.thread_id
        if not thread_id:
            raise RefusalError(SessionKind.NO_THREAD, "no active thread")

        return cls(user_id=str(user_id), thread_id=thread_id)
