"""Вход по паролю и общая сборка результата провайдера.

Local и LDAP проверяют credentials разными способами; `SignedInFactory`
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
from dataclasses import dataclass
from enum import StrEnum

from pydantic import SecretStr

from boba.auth.config import LdapAuthConfig, LocalAuthConfig
from boba.auth.profiles import ProfileProviders
from boba.auth.realms import RealmSelection
from boba.auth.roles import RoleProviders
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
from boba.identity.principal import UserIdentity
from boba.identity.session import (
    LoginTemplate,
    UserLogin,
)
from boba.identity.signin import PasswordSignIn, SignedIn, SignInMetadata

__all__ = [
    "CompositeSignIn",
    "LdapSignIn",
    "LocalSignIn",
    "LoginField",
    "SignInDetails",
    "SignedInFactory",
]


class LoginField(StrEnum):
    """Поля формы входа по паролю, её адреса и cookie страницы входа: общий
    контракт сервера со страницей входа (boba-mcp) и клиента, который
    проходит её без человека (boba-mcp-client)."""

    TXN = "txn"
    LOGIN = "login"
    PASSWORD = "password"  # noqa: S105 — имя поля формы
    PROOF = "proof"
    REALM = "realm"
    COOKIE = "boba_mcp_login"
    SSO = "/login/sso"


@dataclass(frozen=True)
class SignInDetails:
    """Проверенная личность и факты, из которых собирается итог входа."""

    provider: str
    identity: UserIdentity
    display_name: str
    facts: PrincipalFacts
    principal: str = ""
    sealed_ticket: str = ""
    renewal: str = ""
    credential_reason: str = (
        "This sign-in supplied no delegated credentials; "
        "use Kerberos SSO for delegated connections."
    )


class SignedInFactory:
    """Общий выход провайдера после проверки учётных данных.

    Получает уже подтверждённую личность и факты входа, применяет единые
    правила ролей и профилей и формирует `SignedIn` для AuthService и OAuth.
    """

    def __init__(self, roles: RoleProviders, profiles: ProfileProviders) -> None:
        self._roles = roles
        self._profiles = profiles

    async def create(self, details: SignInDetails) -> SignedIn:
        roles = frozenset(await self._roles.admit(details.facts, details.identity))
        grant = await self._profiles.granted(details.facts, roles)
        sign_in = SignInMetadata(
            provider=details.provider,
            renewal=details.renewal,
            credential_reason=details.credential_reason,
            identity=details.identity,
            principal=details.principal,
            sealed_ticket=details.sealed_ticket,
            roles=roles,
            profiles=grant.granted,
            profile=grant.selected,
        )

        return SignedIn(
            identifier=details.identity.identifier,
            display_name=details.display_name,
            sign_in=sign_in,
        )


class LocalSignIn(PasswordSignIn):
    """Вход по статической таблице логин/пароль из конфига."""

    def __init__(
        self, config: LocalAuthConfig, roles: RoleProviders, profiles: ProfileProviders
    ) -> None:
        self._config = config
        self._signed_in = SignedInFactory(roles, profiles)

    @property
    def realms(self) -> frozenset[str]:
        return frozenset({self._config.identity.realm})

    async def sign_in(
        self, username: str, password: str, realm: str = ""
    ) -> SignedIn | None:
        if realm not in ("", self._config.identity.realm):
            return None

        if self._config.users.get(username) != password:
            return None

        facts = PrincipalFacts(login=username)
        login = UserLogin.of(username)
        identity = self._config.identity.identity(login.key)
        return await self._signed_in.create(
            SignInDetails(
                provider=self._config.PROVIDER,
                identity=identity,
                display_name=login.display,
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
        self._signed_in = SignedInFactory(roles, profiles)
        self._logger = logging.getLogger(__name__)

    @property
    def realms(self) -> frozenset[str]:
        return frozenset({self._config.identity.realm})

    async def sign_in(
        self, username: str, password: str, realm: str = ""
    ) -> SignedIn | None:
        if realm not in ("", self._config.identity.realm):
            return None

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
                display_name=login.display,
                facts=facts,
            )
        )


class CompositeSignIn(PasswordSignIn):
    """Провайдеры по порядку конфига: первый узнавший логин решает."""

    def __init__(self, providers: Sequence[PasswordSignIn]) -> None:
        self._providers = list(providers)
        self._selection = RealmSelection()

    @property
    def realms(self) -> frozenset[str]:
        realms: set[str] = set()
        for provider in self._providers:
            realms.update(provider.realms)

        return frozenset(realms)

    async def sign_in(
        self, username: str, password: str, realm: str = ""
    ) -> SignedIn | None:
        selected = self._selection.selected(realm, self.realms, "password sign-in")
        last_error: AuthenticationError | None = None

        for provider in self._providers:
            if selected not in provider.realms:
                continue

            try:
                signed = await provider.sign_in(username, password, selected)
            except AuthenticationError as exc:
                last_error = exc
                continue

            if signed is not None:
                return signed

        if last_error is not None:
            raise last_error

        return None
