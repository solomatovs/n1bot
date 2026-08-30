"""Вход по паролю и его итог: metadata входа моделью, порт провайдера.

Ошибки (выпускают реализации PasswordSignIn):
AuthenticationError — логин не зарегистрирован или пароль неверен.
AuthorizationError — вход запрещён: исключение или ни одной роли.
ExternalServiceError — каталог недоступен.
InternalServiceError — ошибка конфига или каталога на нашей стороне.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from boba.identity.context import Credential, DelegatedTicket, NoUserCredential
from boba.identity.session import SignInProvider, UserMetadataField

__all__ = ["PasswordSignIn", "SignInMetadata", "SignedIn"]


class SignInMetadata(BaseModel):
    """Что вход знает о себе: провайдер, роли, принципал SSO и запечатанный билет.

    Единственная модель этих ключей: из словаря chainlit, из claims JWT и из
    строки users читается она же. Рендер отдаёт только заполненные ключи.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = ""
    roles: frozenset[str] = frozenset()
    principal: str = ""
    sealed_ticket: str = ""

    @classmethod
    def parse(cls, raw: Mapping[str, object]) -> SignInMetadata:
        """Разбор словаря входа: чужие ключи (llm, studio_profile) не читает."""
        return cls(
            provider=cls._text(raw.get(UserMetadataField.PROVIDER)),
            roles=cls._roles_in(raw.get(UserMetadataField.ROLES)),
            principal=cls._text(raw.get(UserMetadataField.PRINCIPAL)),
            sealed_ticket=cls._text(raw.get(UserMetadataField.TICKET)),
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

        return rendered

    def persistable(self) -> SignInMetadata:
        """То, что можно хранить в строке users: без билета входа."""
        return self.model_copy(update={"sealed_ticket": ""})

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
                "connection acts in the database on your behalf: sign in with "
                "the Kerberos SSO button instead"
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
    def _roles_in(value: object) -> frozenset[str]:
        """Роли строкой, перечнем либо ничем."""
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

    identifier: str
    display_name: str
    sign_in: SignInMetadata


class PasswordSignIn(Protocol):
    """Провайдер входа по логину и паролю; None — логин провайдеру неизвестен."""

    @abstractmethod
    async def sign_in(self, username: str, password: str) -> SignedIn | None: ...
