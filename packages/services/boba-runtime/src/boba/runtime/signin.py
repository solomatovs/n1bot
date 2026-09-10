"""Сборка входов из конфига: единственное место, где реализации провайдеров
ролей и профилей встречаются с входами. Каждый тип входа получает композиты
по своим секциям roles.<провайдер> и profiles.<провайдер>.

Ошибки: свои не выпускает.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from boba.auth.config import (
    AuthConfig,
    KerberosAuthConfig,
    KerberosRoleProviders,
    LdapAuthConfig,
    LdapRoleProviders,
    LocalAuthConfig,
    LocalRoleProviders,
    ProxyAuthConfig,
    ProxyProfileProviders,
    ProxyRoleProviders,
)
from boba.auth.profiles import (
    HeaderProfiles,
    ProfileProvider,
    ProfileProviders,
    RoleProfiles,
)
from boba.auth.proxy import HmacProxySignIn
from boba.auth.roles import (
    DirectoryLookup,
    DirectoryRoles,
    HeaderRoles,
    LdapRoles,
    LocalRoles,
    MissingEntry,
    PrincipalRoles,
    RoleProvider,
    RoleProviders,
)
from boba.auth.signin import CompositeSignIn, LdapSignIn, LocalSignIn
from boba.auth.sso import SsoSignIn
from boba.chat.profiles import ChatProfiles
from boba.identity.directory import UserDirectory
from boba.identity.signin import PasswordSignIn

__all__ = ["SignInAssembly"]


class SignInAssembly:
    """Входы приложения по [auth]: пароли, SPNEGO и proxy с их провайдерами."""

    def __init__(self, directory: UserDirectory, profiles: ChatProfiles) -> None:
        self._directory = directory
        self._profiles = profiles

    def password(self, configs: Sequence[AuthConfig]) -> CompositeSignIn | None:
        """Провайдеры паролей из [auth]: local и ldap по порядку конфига."""
        providers = list(self._password_of(configs))
        if not providers:
            return None

        return CompositeSignIn(providers)

    def sso(self, config: KerberosAuthConfig, secret: str) -> SsoSignIn:
        roles = RoleProviders(
            list(self._kerberos_roles(config.roles)), config.require_roles
        )

        return SsoSignIn(config, secret, roles, self._by_roles())

    def proxy(self, config: ProxyAuthConfig) -> HmacProxySignIn:
        roles = RoleProviders(
            list(self._proxy_roles(config.roles)), config.require_roles
        )
        profiles = ProfileProviders(list(self._proxy_profiles(config.profiles)))

        return HmacProxySignIn(config, roles, profiles)

    def _password_of(self, configs: Sequence[AuthConfig]) -> Iterator[PasswordSignIn]:
        for config in configs:
            if isinstance(config, LocalAuthConfig):
                roles = RoleProviders(
                    list(self._local_roles(config.roles)), config.require_roles
                )
                yield LocalSignIn(config, roles, self._by_roles())

            if isinstance(config, LdapAuthConfig):
                roles = RoleProviders(
                    list(self._ldap_roles(config.roles)), config.require_roles
                )
                yield LdapSignIn(config, self._directory, roles, self._by_roles())

    def _by_roles(self) -> ProfileProviders:
        """Профили по ролям: единственный провайдер у local, ldap и kerberos."""
        return ProfileProviders([RoleProfiles(self._profiles)])

    @staticmethod
    def _local_roles(config: LocalRoleProviders) -> Iterator[RoleProvider]:
        if config.local is not None:
            yield LocalRoles(config.local)

    @staticmethod
    def _ldap_roles(config: LdapRoleProviders) -> Iterator[RoleProvider]:
        if config.directory is not None:
            yield DirectoryRoles(config.directory)

    def _kerberos_roles(self, config: KerberosRoleProviders) -> Iterator[RoleProvider]:
        if config.principal is not None:
            yield PrincipalRoles(config.principal)

        if config.ldap is not None:
            yield LdapRoles(
                config.ldap,
                self._directory,
                DirectoryLookup.UPN,
                "sso roles",
                MissingEntry.REFUSE,
            )

    def _proxy_roles(self, config: ProxyRoleProviders) -> Iterator[RoleProvider]:
        if config.local is not None:
            yield LocalRoles(config.local)

        if config.ldap is not None:
            yield LdapRoles(
                config.ldap,
                self._directory,
                DirectoryLookup.SAM_ACCOUNT_NAME,
                "proxy roles",
                MissingEntry.SKIP,
            )

        if config.header is not None:
            yield HeaderRoles(config.header)

    def _proxy_profiles(
        self, config: ProxyProfileProviders
    ) -> Iterator[ProfileProvider]:
        yield RoleProfiles(self._profiles)

        if config.header is not None:
            yield HeaderProfiles(config.header, self._profiles)
