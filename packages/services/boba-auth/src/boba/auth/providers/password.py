"""Вход по паролю и общая сборка результата провайдера.

Local и LDAP проверяют credentials разными способами; `SignInResultFactory`
применяет к результату одинаковые правила ролей и профилей.

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

from boba.auth.config import LdapAuthConfig, LocalAuthConfig
from boba.auth.providers.profiles import ProfileProviders
from boba.auth.providers.result import SignInDetails, SignInResultFactory
from boba.auth.providers.roles import RoleProviders
from boba.identity.admission import PrincipalFacts
from boba.identity.directory import (
    DirectoryBinding,
    DirectorySearch,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
    SimpleBind,
    UserDirectory,
)
from boba.identity.errors import (
    AuthenticationError,
    ExternalServiceError,
    InternalServiceError,
)
from boba.identity.session import (
    LoginTemplate,
    UserLogin,
)
from boba.identity.signin import PasswordSignIn, SignInResult


class LocalSignIn(PasswordSignIn):
    """Вход по статической таблице логин/пароль из конфига."""

    def __init__(
        self, config: LocalAuthConfig, roles: RoleProviders, profiles: ProfileProviders
    ) -> None:
        self._config = config
        self._signed_in = SignInResultFactory(roles, profiles)

    async def sign_in(self, username: str, password: str) -> SignInResult | None:
        if self._config.users.get(username) != password:
            return None

        facts = PrincipalFacts(login=username)
        login = UserLogin.of(username)
        identity = self._config.identity.identity(login.key)
        return await self._signed_in.create(
            SignInDetails(
                provider=self._config.PROVIDER,
                identity=identity,
                facts=facts,
            )
        )


class LdapSignIn(PasswordSignIn):
    """Логин/пароль с проверкой bind'ом в AD; роли — по атрибутам каталога."""

    def __init__(
        self,
        config: LdapAuthConfig,
        directory: UserDirectory,
        roles: RoleProviders,
        profiles: ProfileProviders,
    ) -> None:
        self._config = config
        self._directory = directory
        self._signed_in = SignInResultFactory(roles, profiles)
        self._logger = logging.getLogger(__name__)

    async def sign_in(self, username: str, password: str) -> SignInResult | None:
        # личность подтверждаем bind'ом под пользователем
        bind_user = LoginTemplate.render(self._config.bind_dn_template, username)
        auth = SimpleBind(method="simple", user=bind_user, password=SecretStr(password))
        binding = DirectoryBinding(server=self._config.server, auth=auth)
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
                bind_user,
                server,
                e,
            )
            message = (
                f"Invalid username or password: bind as {bind_user} "
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
        login = UserLogin.of(entry.samaccountname)
        identity = self._config.identity.identity(login.key)
        return await self._signed_in.create(
            SignInDetails(
                provider=self._config.PROVIDER,
                identity=identity,
                facts=facts,
            )
        )


class CompositeSignIn(PasswordSignIn):
    """Провайдеры по порядку конфига: первый подтвердивший credentials решает."""

    def __init__(self, providers: Sequence[PasswordSignIn]) -> None:
        self._providers = list(providers)

    async def sign_in(self, username: str, password: str) -> SignInResult | None:
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
