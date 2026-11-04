"""Вход по паролю и по доверенному заголовку: типизированный результат,
порт провайдера, запрос proxy-входа глазами сервиса.

Ошибки (выпускают реализации PasswordSignIn):
AuthenticationError — логин не зарегистрирован или пароль неверен.
AuthorizationError — вход запрещён: исключение или ни одной роли.
ExternalServiceError — каталог недоступен.
InternalServiceError — ошибка конфига или каталога на нашей стороне.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.identity.authentication import AccessGrant, RenewalKind
from boba.identity.context import Credential
from boba.identity.principal import UserIdentity
from boba.identity.session import Login

__all__ = [
    "PasswordSignIn",
    "ProfileCatalog",
    "ProxyHeaderNames",
    "ProxyRequest",
    "ProxySignIn",
    "SessionAccess",
    "SignInResult",
]


class SignInResult(BaseModel):
    """Результат провайдера до сохранения пользователя и выпуска сессии.

    managed_roles используется только для синхронизации членства в БД;
    provider — для журнала входа, preferred_profile — подсказка интерфейсу.
    Эти поля не входят в токен.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    identity: UserIdentity
    access: AccessGrant
    credential: Credential
    renewal: RenewalKind
    provider: str = Field(min_length=1)
    managed_roles: frozenset[str] | None = None
    preferred_profile: str = ""

    @property
    def identifier(self) -> Login:
        return self.identity.identifier

    @model_validator(mode="after")
    def _profile_is_granted(self) -> SignInResult:
        if (
            self.preferred_profile
            and self.preferred_profile not in self.access.profiles
        ):
            raise ValueError("the preferred profile must be granted to the sign-in")
        return self


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

    def current(self, access: AccessGrant, roles: frozenset[str]) -> AccessGrant:
        profiles = access.profiles
        if not roles:
            profiles = frozenset()
        elif self._profiles is not None and roles != access.roles:
            # При неизменном членстве сохраняются и явные grants proxy-входа.
            profiles &= self._profiles.granted_by_roles(roles)
        return AccessGrant(roles=roles, profiles=profiles)


class ProxyHeaderNames(BaseModel):
    """Какие заголовки транспорт читает в ProxyRequest: логин, метка времени,
    подпись, роли и профили. Пустое имя — заголовок не читается, поле остаётся
    пустой строкой. Собирается конфигом [auth.proxy] и его провайдерами."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

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

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

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
    ) -> SignInResult | None: ...


class ProxySignIn(Protocol):
    """Провайдер входа по доверенному запросу бэкенда партнёра."""

    @abstractmethod
    async def sign_in(self, request: ProxyRequest) -> SignInResult:
        """AuthenticationError — запрос не подтверждён; AuthorizationError — вход
        запрещён."""

    @abstractmethod
    async def admit(self, request: ProxyRequest) -> SignInResult:
        """Допуск по запросу, который вызывающий уже подтвердил сам;
        AuthorizationError — вход запрещён."""
