"""Сессия чата в терминах домена: контракт, метки входа и лога.

Транспорта здесь нет: chainlit живёт в реализации (infra/session.py), а
логика знает только этот протокол. Так работа с сессией остаётся в одном
месте, не таща за собой веб-фреймворк.

Ошибки:
TemplateError — шаблон входа без {username} либо принципал не по шаблону.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final, Protocol

from boba.toolkit.template import FieldTemplate, TemplateError

__all__ = [
    "LogLine",
    "LogUserMark",
    "LoginTemplate",
    "Session",
    "SessionSource",
    "SignInProvider",
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


class SignInProvider(StrEnum):
    """Значение metadata[provider]: каким провайдером выпущен вход."""

    KERBEROS = "KerberosAuth"
    LDAP = "LdapAuth"
    LOCAL = "LocalAuth"


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


class LoginTemplate:
    """Шаблоны входа с полем {username}: формат принципала, bind DN, LDAP-фильтр.

    Одно место для подстановки логина в шаблон и обратного разбора логина из
    принципала; провайдеры входа сами шаблоны не разбирают.
    """

    FIELD: ClassVar[str] = "username"

    @classmethod
    def check(cls, text: str) -> str:
        """Валидатор конфига: шаблон разбирается и содержит {username}."""
        try:
            FieldTemplate.parse(text).having(cls.FIELD)
        except TemplateError as exc:
            raise ValueError(str(exc)) from exc

        return text

    @classmethod
    def check_principal(cls, text: str) -> str:
        """Валидатор формата принципала: {username} ровно один и извлекаем."""
        try:
            FieldTemplate.parse(text).single(cls.FIELD)
        except TemplateError as exc:
            raise ValueError(str(exc)) from exc

        return text

    @classmethod
    def render(cls, text: str, username: str) -> str:
        return FieldTemplate.parse(text).render({cls.FIELD: username})

    @classmethod
    def username_of(cls, text: str, principal: str) -> str:
        """Логин из принципала по шаблону; несоответствие — TemplateError."""
        return FieldTemplate.parse(text).extract(principal, cls.FIELD)


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

    @property
    def label(self) -> str:
        return self._label

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


class SessionSource(Protocol):
    """Откуда берётся сессия текущего вызова."""

    def current(self) -> Session: ...
