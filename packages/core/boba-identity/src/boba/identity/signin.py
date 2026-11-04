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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.identity.context import Credential, DelegatedTicket, NoUserCredential
from boba.identity.principal import UserIdentity
from boba.identity.session import Login, UserMetadataField

__all__ = [
    "PasswordSignIn",
    "ProfileCatalog",
    "ProxyHeaderNames",
    "ProxyRequest",
    "ProxySignIn",
    "SessionAccess",
    "SignInMetadata",
    "SignedIn",
]


class SignInMetadata(BaseModel):
    """Что вход знает о себе: провайдер, роли, профили и выбранный профиль,
    принципал SSO, запечатанный билет и поколение сессий, при котором вход
    выпущен.

    Одна модель для словаря Chainlit и claims JWT. В users хранятся только
    личность и членство в ролях. Рендер отдаёт только заполненные ключи.
    Поколение живёт в metadata, а не в claims, потому что cookie парольного
    входа выпускает chainlit из cl.User, и metadata — единственное, что он
    переносит в токен целиком.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = ""
    identity: UserIdentity | None = None
    roles: frozenset[str] = frozenset()
    profiles: frozenset[str] = frozenset()
    profile: str = ""
    """Профиль, выбранный входом для новых чатов; пустой — выбора нет."""
    principal: str = ""
    sealed_ticket: str = ""
    generation: str = ""
    renewal: str = "reauthenticate"
    credential_reason: str = "The sign-in supplied no delegated credentials."

    def with_access(
        self, roles: frozenset[str], profiles: frozenset[str]
    ) -> SignInMetadata:
        """Обновляет права, сохраняя личность и credentials сессии."""
        selected = self.profile
        if selected not in profiles:
            selected = ""

        return self.model_copy(
            update={"roles": roles, "profiles": profiles, "profile": selected}
        )

    @classmethod
    def parse(cls, raw: Mapping[str, object]) -> SignInMetadata:
        """Разбор словаря входа: чужие ключи не читает."""
        identity = None
        value = raw.get(UserMetadataField.IDENTITY)
        if value is not None:
            identity = UserIdentity.model_validate(value)

        return cls(
            provider=cls._text(raw.get(UserMetadataField.PROVIDER)),
            renewal=cls._text(raw.get(UserMetadataField.RENEWAL, "reauthenticate")),
            credential_reason=cls._text(raw.get(UserMetadataField.CREDENTIAL_REASON)),
            identity=identity,
            roles=cls._names_in(raw.get(UserMetadataField.ROLES)),
            profiles=cls._names_in(raw.get(UserMetadataField.PROFILES)),
            profile=cls._text(raw.get(UserMetadataField.PROFILE)),
            principal=cls._text(raw.get(UserMetadataField.PRINCIPAL)),
            sealed_ticket=cls._text(raw.get(UserMetadataField.TICKET)),
            generation=cls._text(raw.get(UserMetadataField.GENERATION)),
        )

    def render(self) -> dict[str, object]:
        """Ключи UserMetadataField для cl.User и claims JWT."""
        rendered: dict[str, object] = {UserMetadataField.RENEWAL: self.renewal}
        if self.credential_reason:
            rendered[UserMetadataField.CREDENTIAL_REASON] = self.credential_reason

        if self.provider:
            rendered[UserMetadataField.PROVIDER] = self.provider

        if self.identity is not None:
            rendered[UserMetadataField.IDENTITY] = self.identity.model_dump(mode="json")

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

    def identity_for(self, identifier: str) -> UserIdentity:
        """Подтверждённая личность именно этого идентификатора входа."""
        if self.identity is None:
            msg = (
                f"sign-in of {identifier!r}: identity origin is missing; "
                "sign out and sign in again"
            )
            raise ValueError(msg)

        if not self.identity.matches(identifier):
            msg = (
                f"sign-in of {identifier!r}: identity names "
                f"{self.identity.identifier!r}; expected the same user"
            )
            raise ValueError(msg)

        return self.identity

    def issued_at(self, generation: str) -> SignInMetadata:
        """Тот же вход, помеченный текущим поколением сессий."""
        return self.model_copy(update={"generation": generation})

    def ticket(self) -> DelegatedTicket | None:
        """Подтверждённый провайдером билет, независимо от способа входа."""
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
        """Причина отсутствия credentials, которую сообщил провайдер входа."""
        if self.credential_reason:
            return self.credential_reason

        return "The sign-in supplied no delegated credentials; sign in again."

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


class ProfileCatalog(Protocol):
    """Профили приложения глазами входа: какие выданы ролям и какие имена
    существуют. У чата это профили [profiles.*], у сервиса boba-mcp — его
    endpoint'ы; провайдеры профилей входа знают только этот протокол."""

    @abstractmethod
    def granted_by_roles(self, user_roles: frozenset[str]) -> frozenset[str]:
        """Имена профилей, выданных ролям входа."""

    @abstractmethod
    def known(self, name: str) -> bool:
        """Есть ли профиль с таким именем."""


class SessionAccess:
    """Сверяет права сессии с текущими ролями для входа AID и OAuth служб.

    Каталог профилей передаёт сборка входа; класс не читает БД и токены.
    Обновление может сузить ранее выданные профили, но не расширить их.
    """

    def __init__(self, profiles: ProfileCatalog | None) -> None:
        self._profiles = profiles

    def current(self, sign_in: SignInMetadata, roles: frozenset[str]) -> SignInMetadata:
        profiles = sign_in.profiles
        if not roles:
            return sign_in.with_access(roles, frozenset())

        if roles == sign_in.roles:
            return sign_in.with_access(roles, profiles)

        if self._profiles is not None:
            profiles &= self._profiles.granted_by_roles(roles)

        return sign_in.with_access(roles, profiles)

    def resource(
        self, sign_in: SignInMetadata, roles: frozenset[str]
    ) -> SignInMetadata:
        """Области ресурса дополнительно ограничены его каталогом разрешений."""
        current = self.current(sign_in, roles)
        if self._profiles is None:
            return current

        profiles = current.profiles & self._profiles.granted_by_roles(roles)

        return current.with_access(roles, profiles)


class SignedIn(BaseModel):
    """Кто вошёл: ключ строки users, отображаемое имя и metadata входа."""

    model_config = ConfigDict(frozen=True)

    identifier: Login
    display_name: str
    sign_in: SignInMetadata

    @model_validator(mode="after")
    def _identity_matches_identifier(self) -> SignedIn:
        if self.sign_in.identity is not None:
            self.sign_in.identity_for(self.identifier)

        return self


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
    identity: UserIdentity | None = None

    def payload(self) -> str:
        """Что подписывает бэкенд: login:timestamp:roles:profiles:profile."""
        return self.SEPARATOR.join(
            (self.login, self.timestamp, self.roles, self.profiles, self.profile)
        )


class PasswordSignIn(Protocol):
    """Провайдер входа по логину и паролю; None — логин провайдеру неизвестен."""

    @property
    @abstractmethod
    def realms(self) -> frozenset[str]:
        """Пространства пользователей, которые обслуживает этот вход."""

    @abstractmethod
    async def sign_in(
        self, username: str, password: str, realm: str = ""
    ) -> SignedIn | None: ...


class ProxySignIn(Protocol):
    """Провайдер входа по доверенному запросу бэкенда партнёра."""

    @abstractmethod
    async def sign_in(self, request: ProxyRequest) -> SignedIn:
        """AuthenticationError — запрос не подтверждён; AuthorizationError — вход
        запрещён."""

    @abstractmethod
    async def admit(self, request: ProxyRequest) -> SignedIn:
        """Допуск по запросу, который вызывающий уже подтвердил сам;
        AuthorizationError — вход запрещён."""
