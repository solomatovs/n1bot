"""Вход по паролю: статическая таблица и bind в каталоге через порт UserDirectory;
роли — по правилам конфига.

Ошибки:
AuthenticationError — логин не зарегистрирован или пароль неверен.
AuthorizationError — вход запрещён: исключение или ни одной роли.
ExternalServiceError — LDAP недоступен.
InternalServiceError — ошибка LDAP-конфига или каталога.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import SecretStr

from boba.auth.config import AuthConfig, LdapAuthConfig, LocalAuthConfig
from boba.identity.admission import PrincipalFacts
from boba.identity.directory import (
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
    ExternalServiceError,
    InternalServiceError,
)
from boba.identity.session import (
    LoginTemplate,
    SignInProvider,
    UserLogin,
)
from boba.identity.signin import PasswordSignIn, SignedIn, SignInMetadata

__all__ = [
    "CompositeSignIn",
    "LdapSignIn",
    "LocalSignIn",
    "PasswordSignIns",
]


class LocalSignIn(PasswordSignIn):
    """Вход по статической таблице логин/пароль из конфига."""

    def __init__(self, config: LocalAuthConfig) -> None:
        self._config = config
        self._rules = config.rules()

    async def sign_in(self, username: str, password: str) -> SignedIn | None:
        if self._config.users.get(username) != password:
            return None

        roles = self._rules.admit(PrincipalFacts(login=username))
        sign_in = SignInMetadata(
            provider=SignInProvider.LOCAL.value, roles=frozenset(roles)
        )

        login = UserLogin.of(username)

        return SignedIn(
            identifier=login.key, display_name=login.display, sign_in=sign_in
        )


class LdapSignIn(PasswordSignIn):
    """Логин/пароль с проверкой bind'ом в AD; роли — по атрибутам каталога."""

    def __init__(self, config: LdapAuthConfig, directory: UserDirectory) -> None:
        self._config = config
        self._directory = directory
        self._rules = config.rules()
        self._logger = logging.getLogger(__name__)

    async def sign_in(self, username: str, password: str) -> SignedIn | None:
        # личность подтверждаем bind'ом под пользователем
        binding = DirectoryBinding(
            server=self._config.server,
            bind_dn=LoginTemplate.render(self._config.bind_dn_template, username),
            bind_password=SecretStr(password),
        )
        search = DirectorySearch(
            base_dn=self._config.base_dn,
            filter=LoginTemplate.render(self._config.user_filter, username),
        )
        server = self._config.server
        try:
            entry = await self._directory.find(binding, search)
        except LDAPUserNotFoundError as e:
            self._logger.warning(
                "ldap sign-in of %r: no entry matching %s under %s on %s: %s",
                username,
                search.filter,
                search.base_dn,
                server,
                e,
            )
            message = (
                f"User {username!r} is not registered: no entry matching "
                f"{search.filter} under {search.base_dn} on {server}"
            )
            raise AuthenticationError(message) from e
        except LDAPInvalidCredentialsError as e:
            self._logger.warning(
                "ldap sign-in of %r: bind as %s on %s rejected: %s",
                username,
                binding.bind_dn,
                server,
                e,
            )
            message = (
                f"Invalid username or password: bind as {binding.bind_dn} "
                f"on {server} rejected"
            )
            raise AuthenticationError(message) from e
        except LDAPServerUnavailableError as e:
            self._logger.error(
                "ldap sign-in of %r: server %s is unavailable: %s",
                username,
                server,
                e,
                exc_info=e,
            )
            message = (
                f"LDAP server {server} is unavailable, please try again later: {e}"
            )
            raise ExternalServiceError("ldap", message) from e
        except LDAPError as e:
            # access denied / кривой конфиг / прочее — наша вина
            self._logger.error(
                "ldap sign-in of %r: search %s under %s on %s failed: %s",
                username,
                search.filter,
                search.base_dn,
                server,
                e,
                exc_info=e,
            )
            detail = (
                f"ldap sign-in of {username!r}: search {search.filter} under "
                f"{search.base_dn} on {server} failed: {e}"
            )
            raise InternalServiceError(internal_detail=detail, user_detail=None) from e

        # имя берём из каталога, а не из формы: набранный регистр на
        # роли, запреты и строку users влиять не должен
        facts = PrincipalFacts(
            login=entry.samaccountname, dn=entry.dn, member_of=tuple(entry.member_of)
        )
        roles = self._rules.admit(facts)
        sign_in = SignInMetadata(
            provider=SignInProvider.LDAP.value, roles=frozenset(roles)
        )

        login = UserLogin.of(entry.samaccountname)

        return SignedIn(
            identifier=login.key, display_name=login.display, sign_in=sign_in
        )


class CompositeSignIn(PasswordSignIn):
    """Провайдеры по порядку конфига: первый узнавший логин решает."""

    def __init__(self, providers: Sequence[PasswordSignIn]) -> None:
        self._providers = list(providers)

    async def sign_in(self, username: str, password: str) -> SignedIn | None:
        last_error: AuthenticationError | None = None

        for provider in self._providers:
            try:
                signed = await provider.sign_in(username, password)
            except AuthenticationError as exc:
                last_error = exc
                continue

            if signed is not None:
                return signed

        if last_error is not None:
            raise last_error

        return None


class PasswordSignIns:
    """Провайдеры паролей из [auth]: local и ldap; kerberos сюда не входит."""

    @classmethod
    def of(
        cls, configs: Sequence[AuthConfig], directory: UserDirectory
    ) -> CompositeSignIn | None:
        providers: list[PasswordSignIn] = []
        for config in configs:
            if isinstance(config, LocalAuthConfig):
                providers.append(LocalSignIn(config))

            if isinstance(config, LdapAuthConfig):
                providers.append(LdapSignIn(config, directory))

        if not providers:
            return None

        return CompositeSignIn(providers)
