"""Провайдеры ролей входа: протокол, реализации по секциям roles.<провайдер>
конфига и композит, который складывает роли, отказывает по исключениям и
проверяет порог require_roles на объединении. Какие реализации собрать —
решает bootstrap приложения.

Ошибки:
AuthorizationError — исключение по любому провайдеру или ни одной роли при
    require_roles.
AuthenticationError — провайдер ldap не нашёл пользователя в каталоге.
ExternalServiceError — каталог недоступен.
InternalServiceError — служебный bind или поиск в каталоге отвергнуты.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from boba.auth.config import (
    DirectoryRolesConfig,
    HeaderRolesConfig,
    LdapRolesConfig,
    LocalRolesConfig,
    PrincipalRolesConfig,
)
from boba.identity.admission import PrincipalFacts, RoleRules
from boba.identity.directory import (
    ADUserEntry,
    DirectoryBinding,
    DirectorySearch,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
    UserDirectory,
)
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    ExternalServiceError,
    InternalServiceError,
)

__all__ = [
    "DirectoryLookup",
    "DirectoryRoles",
    "HeaderAttribute",
    "HeaderRoles",
    "LdapRoles",
    "LocalRoles",
    "MissingEntry",
    "PrincipalRoles",
    "RoleProvider",
    "RoleProviders",
]

logger = logging.getLogger(__name__)


class HeaderAttribute(StrEnum):
    """Ключи доверенных атрибутов транспорта в PrincipalFacts.attributes: значения
    заголовков proxy-входа, уже покрытые подписью."""

    ROLES = "roles"
    PROFILES = "profiles"
    PROFILE = "profile"

    @staticmethod
    def names(raw: str) -> frozenset[str]:
        """Имена через запятую: пробелы по краям срезаются, пустые не считаются."""
        names: set[str] = set()
        for piece in raw.split(","):
            name = piece.strip()
            if not name:
                continue

            names.add(name)

        return frozenset(names)


class RoleProvider(Protocol):
    """Источник ролей входа по фактам о вошедшем."""

    @abstractmethod
    async def roles_of(self, facts: PrincipalFacts) -> frozenset[str]:
        """Роли провайдера; AuthorizationError — факты попали под исключение."""


class LocalRoles(RoleProvider):
    """Реализация RoleProvider таблицей логинов конфига."""

    def __init__(self, config: LocalRolesConfig) -> None:
        self._rules = config.rules()

    async def roles_of(self, facts: PrincipalFacts) -> frozenset[str]:
        return frozenset(self._rules.admit(facts))


class DirectoryRoles(RoleProvider):
    """Реализация RoleProvider правилами по фактам записи каталога, которые вход
    уже получил сам (ldap-bind)."""

    def __init__(self, config: DirectoryRolesConfig) -> None:
        self._rules = config.rules()

    async def roles_of(self, facts: PrincipalFacts) -> frozenset[str]:
        return frozenset(self._rules.admit(facts))


class PrincipalRoles(RoleProvider):
    """Реализация RoleProvider правилами по принципалу и SID из PAC."""

    def __init__(self, config: PrincipalRolesConfig) -> None:
        self._rules: RoleRules = config.rules()

    async def roles_of(self, facts: PrincipalFacts) -> frozenset[str]:
        return frozenset(self._rules.admit(facts))


class HeaderRoles(RoleProvider):
    """Реализация RoleProvider значением заголовка доверенного бэкенда."""

    def __init__(self, config: HeaderRolesConfig) -> None:
        self._config = config

    @property
    def header(self) -> str:
        return self._config.name

    async def roles_of(self, facts: PrincipalFacts) -> frozenset[str]:
        raw = facts.attributes.get(HeaderAttribute.ROLES.value, "")

        return HeaderAttribute.names(raw)


class DirectoryLookup(StrEnum):
    """Чем искать пользователя в каталоге под служебным bind'ом."""

    UPN = "(userPrincipalName={value})"
    SAM_ACCOUNT_NAME = "(sAMAccountName={value})"

    def render(self, value: str) -> str:
        return self.value.format(value=value)

    def value_of(self, facts: PrincipalFacts) -> str:
        if self is DirectoryLookup.UPN:
            return facts.principal

        return facts.login


class MissingEntry(StrEnum):
    """Что делать провайдеру ldap, когда пользователя нет в каталоге: kerberos-вход
    без записи отказывает, proxy-вход обходится ролями других провайдеров."""

    REFUSE = "refuse"
    SKIP = "skip"


class LdapRoles(RoleProvider):
    """Реализация RoleProvider поиском записи в каталоге под служебным bind'ом и
    правилами directory по ней. Для входов без пароля пользователя — kerberos
    ищет по UPN, proxy по sAMAccountName."""

    def __init__(
        self,
        config: LdapRolesConfig,
        directory: UserDirectory,
        lookup: DirectoryLookup,
        action: str,
        missing: MissingEntry,
    ) -> None:
        self._config = config
        self._directory = directory
        self._lookup = lookup
        self._action = action
        self._missing = missing
        self._rules = config.mapping.rules()

    async def roles_of(self, facts: PrincipalFacts) -> frozenset[str]:
        value = self._lookup.value_of(facts)
        try:
            entry = await self.request(value)
        except AuthenticationError as exc:
            if self._missing is MissingEntry.REFUSE:
                raise

            logger.info(
                "%s of %s: no directory entry, directory roles skipped: %s",
                self._action,
                value,
                exc,
            )
            return frozenset()

        found = facts.model_copy(
            update={
                "login": entry.samaccountname,
                "dn": entry.dn,
                "member_of": tuple(entry.member_of),
            }
        )

        return frozenset(self._rules.admit(found))

    async def request(self, value: str) -> ADUserEntry:
        binding = DirectoryBinding(
            server=self._config.server,
            bind_dn=self._config.bind_dn,
            bind_password=self._config.bind_password,
        )
        search = DirectorySearch(
            base_dn=self._config.base_dn,
            filter=self._lookup.render(value),
        )

        server = self._config.server
        try:
            return await self._directory.find(binding, search)
        except LDAPUserNotFoundError as e:
            message = (
                f"User {value!r} is not registered: no entry matching "
                f"{search.filter} under {search.base_dn} on {server}"
            )
            raise AuthenticationError(message) from e
        except LDAPServerUnavailableError as e:
            message = (
                f"LDAP server {server} is unavailable, please try again later: {e}"
            )
            raise ExternalServiceError("ldap", message) from e
        except LDAPInvalidCredentialsError as e:
            detail = (
                f"{self._action} of {value!r}: service bind as "
                f"{self._config.bind_dn} on {server} rejected: {e}"
            )
            raise InternalServiceError(internal_detail=detail, user_detail=None) from e
        except LDAPError as e:
            detail = (
                f"{self._action} of {value!r}: search {search.filter} under "
                f"{search.base_dn} on {server} failed: {e}"
            )
            raise InternalServiceError(internal_detail=detail, user_detail=None) from e
        except Exception as e:
            detail = (
                f"{self._action} of {value!r}: directory lookup {search.filter} "
                f"under {search.base_dn} on {server} failed unexpectedly: {e}"
            )
            raise InternalServiceError(internal_detail=detail, user_detail=None) from e


class RoleProviders:
    """Провайдеры одного типа входа: объединение ролей и порог require_roles.

    Какие реализации сюда попадают, решает bootstrap приложения по секциям
    roles.<провайдер> конфига; входы работают только с этим композитом.
    """

    def __init__(self, providers: Sequence[RoleProvider], require_roles: bool) -> None:
        self._providers = list(providers)
        self._require_roles = require_roles

    async def admit(self, facts: PrincipalFacts) -> list[str]:
        roles: set[str] = set()
        for provider in self._providers:
            roles.update(await provider.roles_of(facts))

        if self._require_roles and not roles:
            msg = (
                f"access denied for {facts.label()}: no role came from any of the "
                f"configured providers {self._names()} while require_roles = true"
            )
            logger.warning("%s", msg)
            raise AuthorizationError(msg)

        return sorted(roles)

    def _names(self) -> list[str]:
        names: list[str] = []
        for provider in self._providers:
            names.append(type(provider).__name__)

        return names
