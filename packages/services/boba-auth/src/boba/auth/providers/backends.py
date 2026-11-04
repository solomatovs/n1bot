"""Готовый комплект local, LDAP, Kerberos и proxy для сборки приложения.

Ошибки:
ValueError — неверная конфигурация встроенного бэкенда.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from boba.auth.assembly import AuthBackends, AuthContribution, AuthServices
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
from boba.auth.providers.kerberos import SpnegoGate
from boba.auth.providers.password import LdapSignIn, LocalSignIn
from boba.auth.providers.profiles import (
    HeaderProfiles,
    ProfileProvider,
    ProfileProviders,
    RoleProfiles,
)
from boba.auth.providers.proxy import HmacProxySignIn
from boba.auth.providers.roles import (
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
from boba.auth.providers.sso import SsoSignIn
from boba.core.components import ComponentFactory
from boba.krb import KerberosWorkspace
from boba.krb.seal import SsoTickets, TicketSealer


class StandardAuthBackends:
    """Готовые фабрики входа, которые приложение подключает к своему конфигу."""

    def __init__(self) -> None:
        self.registry = AuthBackends(
            (
                ComponentFactory(LocalAuthConfig, self.local),
                ComponentFactory(LdapAuthConfig, self.ldap),
                ComponentFactory(KerberosAuthConfig, self.kerberos),
                ComponentFactory(ProxyAuthConfig, self.proxy),
            )
        )

    def local(
        self, config: LocalAuthConfig, services: AuthServices
    ) -> AuthContribution:
        bindings = AdmissionBindings(services)
        roles = RoleProviders(
            list(bindings.local_roles(config.roles)),
            config.require_roles,
            services.roles,
        )
        return AuthContribution(
            password=LocalSignIn(config, roles, bindings.by_roles())
        )

    def ldap(self, config: LdapAuthConfig, services: AuthServices) -> AuthContribution:
        bindings = AdmissionBindings(services)
        roles = RoleProviders(
            list(bindings.ldap_roles(config.roles)),
            config.require_roles,
            services.roles,
        )
        return AuthContribution(
            password=LdapSignIn(config, services.directory, roles, bindings.by_roles())
        )

    def kerberos(
        self, config: KerberosAuthConfig, services: AuthServices
    ) -> AuthContribution:
        bindings = AdmissionBindings(services)
        roles = RoleProviders(
            list(bindings.kerberos_roles(config.roles)),
            config.require_roles,
            services.roles,
        )
        sign_in = SsoSignIn(config, roles, bindings.by_roles())
        gate = SpnegoGate(sign_in, services.secret)
        return AuthContribution(sso=gate, renewal=gate)

    def proxy(
        self, config: ProxyAuthConfig, services: AuthServices
    ) -> AuthContribution:
        bindings = AdmissionBindings(services)
        roles = RoleProviders(
            list(bindings.proxy_roles(config.roles)),
            config.require_roles,
            services.roles,
        )
        profiles = ProfileProviders(list(bindings.proxy_profiles(config.profiles)))
        return AuthContribution(proxy=HmacProxySignIn(config, roles, profiles))


class AdmissionBindings:
    """Собирает общие правила ролей и профилей для фабрик встроенных входов."""

    def __init__(self, services: AuthServices) -> None:
        self._services = services

    def by_roles(self) -> ProfileProviders:
        """Профили по ролям: единственный провайдер у local, ldap и kerberos."""
        return ProfileProviders([RoleProfiles(self._services.profiles)])

    @staticmethod
    def local_roles(config: LocalRoleProviders) -> Iterator[RoleProvider]:
        if config.local is not None:
            yield LocalRoles(config.local)

    @staticmethod
    def ldap_roles(config: LdapRoleProviders) -> Iterator[RoleProvider]:
        if config.directory is not None:
            yield DirectoryRoles(config.directory)

    def kerberos_roles(self, config: KerberosRoleProviders) -> Iterator[RoleProvider]:
        if config.principal is not None:
            yield PrincipalRoles(config.principal)

        if config.ldap is not None:
            yield LdapRoles(
                config.ldap,
                self._services.directory,
                DirectoryLookup.UPN,
                "sso roles",
                MissingEntry.REFUSE,
            )

    def proxy_roles(self, config: ProxyRoleProviders) -> Iterator[RoleProvider]:
        if config.local is not None:
            yield LocalRoles(config.local)

        if config.ldap is not None:
            yield LdapRoles(
                config.ldap,
                self._services.directory,
                DirectoryLookup.SAM_ACCOUNT_NAME,
                "proxy roles",
                MissingEntry.SKIP,
            )

        if config.header is not None:
            yield HeaderRoles(config.header)

    def proxy_profiles(
        self, config: ProxyProfileProviders
    ) -> Iterator[ProfileProvider]:
        yield RoleProfiles(self._services.profiles)

        if config.header is not None:
            yield HeaderProfiles(config.header, self._services.profiles)


class KerberosSessionTickets:
    """Создаёт читателя делегированных билетов для настроенных Kerberos-входов."""

    def build(self, configs: Sequence[AuthConfig], secret: str) -> SsoTickets | None:
        if not any(isinstance(config, KerberosAuthConfig) for config in configs):
            return None

        return SsoTickets(TicketSealer(secret), KerberosWorkspace.krb5_config())
