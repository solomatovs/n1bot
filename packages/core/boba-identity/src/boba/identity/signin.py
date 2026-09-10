"""Вход по паролю и по доверенному заголовку, их итог: metadata входа моделью,
порт провайдера, запрос proxy-входа глазами сервиса.

Ошибки (выпускают реализации PasswordSignIn):
AuthenticationError — логин не зарегистрирован или пароль неверен.
AuthorizationError — вход запрещён: исключение или ни одной роли.
ExternalServiceError — каталог недоступен.
InternalServiceError — ошибка конфига или каталога на нашей стороне.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Mapping
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from boba.identity.context import Credential, DelegatedTicket, NoUserCredential
from boba.identity.session import Login, SignInProvider, UserMetadataField

__all__ = [
    "PasswordSignIn",
    "ProxyHeaderNames",
    "ProxyRequest",
    "ProxySignIn",
    "SignInMetadata",
    "SignedIn",
]


class SignInMetadata(BaseModel):
    """Что вход знает о себе: провайдер, роли, профили и выбранный профиль,
    принципал SSO, запечатанный билет и поколение сессий, при котором вход
    выпущен.

    Единственная модель этих ключей: из словаря chainlit, из claims JWT и из
    строки users читается она же. Рендер отдаёт только заполненные ключи.
    Поколение живёт в metadata, а не в claims, потому что cookie парольного
    входа выпускает chainlit из cl.User, и metadata — единственное, что он
    переносит в токен целиком.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = ""
    roles: frozenset[str] = frozenset()
    profiles: frozenset[str] = frozenset()
    profile: str = ""
    """Профиль, выбранный входом для новых чатов; пустой — выбора нет."""
    principal: str = ""
    sealed_ticket: str = ""
    generation: str = ""

    @classmethod
    def parse(cls, raw: Mapping[str, object]) -> SignInMetadata:
        """Разбор словаря входа: чужие ключи (llm, studio_profile) не читает."""
        return cls(
            provider=cls._text(raw.get(UserMetadataField.PROVIDER)),
            roles=cls._names_in(raw.get(UserMetadataField.ROLES)),
            profiles=cls._names_in(raw.get(UserMetadataField.PROFILES)),
            profile=cls._text(raw.get(UserMetadataField.PROFILE)),
            principal=cls._text(raw.get(UserMetadataField.PRINCIPAL)),
            sealed_ticket=cls._text(raw.get(UserMetadataField.TICKET)),
            generation=cls._text(raw.get(UserMetadataField.GENERATION)),
        )

    def render(self) -> dict[str, object]:
        """Ключи UserMetadataField для cl.User, claims JWT и строки users."""
        rendered: dict[str, object] = {}
        if self.provider:
            rendered[UserMetadataField.PROVIDER] = self.provider

        if self.principal:
            rendered[UserMetadataField.PRINCIPAL] = self.principal

        if self.sealed_ticket:
            rendered[UserMetadataField.TICKET] = self.sealed_ticket

        if self.roles:
            rendered[UserMetadataField.ROLES] = sorted(self.roles)

        if self.profiles:
            rendered[UserMetadataField.PROFILES] = sorted(self.profiles)

        if self.profile:
            rendered[UserMetadataField.PROFILE] = self.profile

        if self.generation:
            rendered[UserMetadataField.GENERATION] = self.generation

        return rendered

    def persistable(self) -> SignInMetadata:
        """То, что можно хранить в строке users: без билета, поколения и выбора
        профиля — они принадлежат сессии, а не пользователю."""
        return self.model_copy(
            update={"sealed_ticket": "", "generation": "", "profile": ""}
        )

    def issued_at(self, generation: str) -> SignInMetadata:
        """Тот же вход, помеченный текущим поколением сессий."""
        return self.model_copy(update={"generation": generation})

    def is_kerberos(self) -> bool:
        return self.provider == SignInProvider.KERBEROS

    def ticket(self) -> DelegatedTicket | None:
        """Билет SSO-входа; None — делегирования не было."""
        if not self.is_kerberos():
            return None

        if not self.principal:
            return None

        if not self.sealed_ticket:
            return None

        return DelegatedTicket(principal=self.principal, sealed=self.sealed_ticket)

    def credential(self) -> Credential:
        """Секреты вызова: билет либо причина его отсутствия."""
        ticket = self.ticket()
        if ticket is not None:
            return ticket

        return NoUserCredential(reason=self.absence_reason())

    def absence_reason(self) -> str:
        """Почему у входа нет делегированного билета; текст готов для отказа."""
        if not self.is_kerberos():
            return (
                f"you signed in with {self._provider_name()}, and this "
                "connection acts in the database on your behalf: sign in "
                "through Kerberos SSO instead"
            )

        if not self.principal:
            return (
                "your Kerberos sign-in predates delegated connections "
                "(the session token names no principal): sign out and sign in again"
            )

        return (
            f"the Kerberos sign-in of {self.principal} carried no delegated ticket: "
            "either Active Directory does not allow this service to act for "
            "you, or the browser sent no ticket; sign in again from a "
            "domain-joined browser"
        )

    def _provider_name(self) -> str:
        if not self.provider:
            return "no known provider"

        return self.provider

    @staticmethod
    def _text(value: object) -> str:
        if not isinstance(value, str):
            return ""

        return value

    @staticmethod
    def _names_in(value: object) -> frozenset[str]:
        """Имена ролей или профилей строкой, перечнем либо ничем."""
        if not value:
            return frozenset()

        if isinstance(value, str):
            return frozenset({value})

        if not isinstance(value, Iterable):
            return frozenset()

        names: set[str] = set()
        for role in value:
            names.add(str(role))

        return frozenset(names)


class SignedIn(BaseModel):
    """Кто вошёл: ключ строки users, отображаемое имя и metadata входа."""

    model_config = ConfigDict(frozen=True)

    identifier: Login
    display_name: str
    sign_in: SignInMetadata


class ProxyHeaderNames(BaseModel):
    """Какие заголовки транспорт читает в ProxyRequest: логин, метка времени,
    подпись, роли и профили. Пустое имя — заголовок не читается, поле остаётся
    пустой строкой. Собирается конфигом [auth.proxy] и его провайдерами."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user: str = Field(min_length=1)
    timestamp: str = Field(min_length=1)
    signature: str = Field(min_length=1)
    roles: str = ""
    profiles: str = ""
    profile: str = ""


class ProxyRequest(BaseModel):
    """Запрос proxy-входа глазами сервиса: логин, подпись, роли и профили из
    заголовков доверенного бэкенда плюс адрес клиента. Собирается адаптером
    транспорта; какие заголовки читать, знает конфиг [auth.proxy].

    Подпись считается по payload(): логин, метка времени и сырые строки ролей,
    профилей и выбранного профиля через двоеточие, чтобы подменить их было нельзя.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    UNKNOWN_CLIENT: ClassVar[str] = "unknown"
    SEPARATOR: ClassVar[str] = ":"

    login: str = ""
    timestamp: str = ""
    signature: str = ""
    roles: str = ""
    profiles: str = ""
    profile: str = ""
    client: str = UNKNOWN_CLIENT

    def payload(self) -> str:
        """Что подписывает бэкенд: login:timestamp:roles:profiles:profile."""
        return self.SEPARATOR.join(
            (self.login, self.timestamp, self.roles, self.profiles, self.profile)
        )


class PasswordSignIn(Protocol):
    """Провайдер входа по логину и паролю; None — логин провайдеру неизвестен."""

    @abstractmethod
    async def sign_in(self, username: str, password: str) -> SignedIn | None: ...


class ProxySignIn(Protocol):
    """Провайдер входа по доверенному запросу бэкенда партнёра."""

    @abstractmethod
    async def sign_in(self, request: ProxyRequest) -> SignedIn:
        """AuthenticationError — запрос не подтверждён; AuthorizationError — вход
        запрещён."""
