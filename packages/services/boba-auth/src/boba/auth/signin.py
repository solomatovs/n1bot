"""Вход по паролю: статическая таблица и bind в AD; роли — по маппингам конфига.

Ошибки:
AuthenticationError — логин не зарегистрирован или пароль неверен.
AuthorizationError — вход запрещён: исключение или ни одной роли.
ExternalServiceError — LDAP недоступен.
InternalServiceError — ошибка LDAP-конфига или каталога.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from ldap3 import (
    Connection,
    Server,
)
from ldap3.core.exceptions import (
    LDAPBindError,
    LDAPCommunicationError,
    LDAPException,
    LDAPInsufficientAccessRightsResult,
    LDAPInvalidCredentialsResult,
    LDAPInvalidDNSyntaxResult,
    LDAPInvalidFilterError,
    LDAPInvalidServerError,
    LDAPNoSuchObjectResult,
    LDAPServerPoolError,
    LDAPStartTLSError,
    LDAPStrongerAuthRequiredResult,
)

from boba.auth.config import AuthConfig, LdapAuthConfig, LocalAuthConfig
from boba.identity.directory import (
    LDAPAccessDeniedError,
    LDAPConfigError,
    LDAPError,
    LDAPInvalidCredentialsError,
    LDAPServerUnavailableError,
    LDAPUserNotFoundError,
)
from boba.identity.errors import (
    AuthenticationError,
    AuthorizationError,
    ExternalServiceError,
    InternalServiceError,
)
from boba.identity.roles import (
    DnExcludeUserProvider,
    DnUserRolesProvider,
    LocalExcludeUserProvider,
    LocalUserRolesProvider,
    MemberOfExcludeUserProvider,
    MemberOfUserRolesProvider,
)
from boba.identity.session import (
    LoginTemplate,
    SignInProvider,
    UserLogin,
    UserMetadataField,
)
from boba.identity.signin import PasswordSignIn, SignedIn

__all__ = [
    "ADDirectory",
    "CompositeSignIn",
    "LdapSignIn",
    "LocalSignIn",
    "PasswordSignIns",
]


class ADDirectory:
    """Каталог AD: поиск пользователя, его группы (memberOf), проверка пароля."""

    @staticmethod
    @contextmanager
    def _bind_with_password(
        server: str,
        bind_dn: str,
        bind_password: str,
        connect_timeout: int = 5,
    ):
        conn: Connection | None = None
        try:
            with Connection(
                server=Server(
                    host=server, get_info="ALL", connect_timeout=connect_timeout
                ),
                user=bind_dn,
                password=bind_password,
                auto_bind="DEFAULT",
                # без raise_exceptions ошибки search'а молча дают пустой результат
                raise_exceptions=True,
            ) as conn:
                yield conn
        except LDAPError:
            # доменные LDAP-ошибки (напр. LDAPUserNotFound из тела with) рейсим как есть
            raise
        except (
            LDAPCommunicationError,
            LDAPInvalidServerError,
            LDAPServerPoolError,
            LDAPStartTLSError,
        ) as e:
            raise LDAPServerUnavailableError(str(e)) from e
        except (LDAPBindError, LDAPInvalidCredentialsResult) as e:
            raise LDAPInvalidCredentialsError(str(e)) from e
        except LDAPInsufficientAccessRightsResult as e:
            raise LDAPAccessDeniedError(str(e)) from e
        except (
            LDAPNoSuchObjectResult,
            LDAPInvalidDNSyntaxResult,
            LDAPInvalidFilterError,
            LDAPStrongerAuthRequiredResult,
        ) as e:
            raise LDAPConfigError(str(e)) from e
        except LDAPException as e:
            raise LDAPError(str(e)) from e
        finally:
            if conn:
                conn.unbind()

    @staticmethod
    def fetch_userdn_and_member_of(
        server: str,
        bind_dn: str,
        bind_password: str,
        search_base: str,
        search_filter: str,
    ) -> tuple[str, list[str]]:
        """Ищет пользователя: (DN, группы memberOf);"""
        with ADDirectory._bind_with_password(
            server,
            bind_dn,
            bind_password,
        ) as conn:
            conn.search(
                search_base=search_base,
                search_filter=search_filter,
                attributes=["memberOf"],
            )

            if not conn.entries:
                raise LDAPUserNotFoundError()

            entry = conn.entries[0]

            dn = str(entry.entry_dn)
            member_of = [str(x) for x in entry.memberOf.values]

            return dn, member_of

    @staticmethod
    def fetch_userdn_samaccountname_member_of(
        server: str,
        bind_dn: str,
        bind_password: str,
        search_base: str,
        search_filter: str,
    ) -> tuple[str, str, list[str]]:
        """Ищет пользователя: (DN, группы memberOf);"""
        with ADDirectory._bind_with_password(
            server,
            bind_dn,
            bind_password,
        ) as conn:
            conn.search(
                search_base=search_base,
                search_filter=search_filter,
                attributes=["sAMAccountName", "memberOf"],
            )

            if not conn.entries:
                raise LDAPUserNotFoundError()

            entry = conn.entries[0]

            dn = str(entry.entry_dn)
            samaccountname = str(entry.sAMAccountName.value)
            member_of = [str(x) for x in entry.memberOf.values]

            return dn, samaccountname, member_of

    @staticmethod
    def role_of(
        group_dn_and_roles: Mapping[str, str], member_of: list[str]
    ) -> Iterable[str]:
        """Возвращает роли которые подключены пользователю"""
        for group_dn, role in group_dn_and_roles.items():
            if group_dn in member_of:
                yield role


class LocalSignIn(PasswordSignIn):
    """Вход по статической таблице логин/пароль из конфига."""

    def __init__(self, config: LocalAuthConfig) -> None:
        self._config = config
        self._init_mapping()

    def _init_mapping(self):
        self._local_roles: LocalUserRolesProvider | None = None
        self._local_roles_ex: LocalExcludeUserProvider | None = None

        if roles := self._config.roles:
            self._local_roles = LocalUserRolesProvider(roles)

        if roles_ex := self._config.roles_ex:
            self._local_roles_ex = LocalExcludeUserProvider(roles_ex)

    async def sign_in(self, username: str, password: str) -> SignedIn | None:
        if self._config.users.get(username) != password:
            return None

        excluded = False
        if self._local_roles_ex:
            excluded = any(self._local_roles_ex.exclude_of(username))

        if excluded:
            raise AuthorizationError("Access denied")

        metadata: dict[str, Any] = {
            UserMetadataField.PROVIDER: SignInProvider.LOCAL.value,
        }

        roles: list[str] = []
        if self._local_roles:
            roles.extend(self._local_roles.roles_of(username))

        roles = sorted(set(roles))

        requires_roles = self._config.require_roles
        if requires_roles and not roles:
            raise AuthorizationError("Access denied")

        if roles:
            metadata[UserMetadataField.ROLES] = roles

        login = UserLogin.of(username)

        return SignedIn(
            identifier=login.key, display_name=login.display, metadata=metadata
        )


class LdapSignIn(PasswordSignIn):
    """Логин/пароль с проверкой bind'ом в AD; роли — из групп AD."""

    def __init__(self, config: LdapAuthConfig):
        self._config = config
        self._ad = ADDirectory
        self._logger = logging.getLogger(__name__)

        self._init_mapping()

    def _init_mapping(self):
        self._samaccountname_roles: LocalUserRolesProvider | None = None
        self._samaccountname_roles_ex: LocalExcludeUserProvider | None = None
        self._member_of_roles: MemberOfUserRolesProvider | None = None
        self._member_of_roles_ex: MemberOfExcludeUserProvider | None = None
        self._dn_roles: DnUserRolesProvider | None = None
        self._dn_roles_ex: DnExcludeUserProvider | None = None

        if roles := self._config.roles.samaccountname:
            self._samaccountname_roles = LocalUserRolesProvider(roles)

        if roles := self._config.roles.samaccountname_ex:
            self._samaccountname_roles_ex = LocalExcludeUserProvider(roles)

        if roles := self._config.roles.member_of:
            self._member_of_roles = MemberOfUserRolesProvider(roles)

        if roles := self._config.roles.member_of_ex:
            self._member_of_roles_ex = MemberOfExcludeUserProvider(roles)

        if roles := self._config.roles.dn:
            self._dn_roles = DnUserRolesProvider(roles)

        if roles := self._config.roles.dn_ex:
            self._dn_roles_ex = DnExcludeUserProvider(roles)

    def _excluded_of(self, username: str, user_dn: str, member_of: list[str]) -> bool:
        return any(self._exclusions_of(username, user_dn, member_of))

    def _exclusions_of(
        self, username: str, user_dn: str, member_of: list[str]
    ) -> Iterator[bool]:
        if self._samaccountname_roles_ex:
            yield from self._samaccountname_roles_ex.exclude_of(username)

        if self._member_of_roles_ex:
            yield from self._member_of_roles_ex.exclude_of(member_of)

        if self._dn_roles_ex:
            yield from self._dn_roles_ex.exclude_of(user_dn)

    def _roles_of(self, username: str, user_dn: str, member_of: list[str]) -> list[str]:
        return sorted(set(self._role_matches(username, user_dn, member_of)))

    def _role_matches(
        self, username: str, user_dn: str, member_of: list[str]
    ) -> Iterator[str]:
        if self._samaccountname_roles:
            yield from self._samaccountname_roles.roles_of(username)

        if self._member_of_roles:
            yield from self._member_of_roles.roles_of(member_of)

        if self._dn_roles:
            yield from self._dn_roles.roles_of(user_dn)

    async def sign_in(self, username: str, password: str) -> SignedIn | None:
        # личность подтверждаем bind'ом под пользователем
        try:
            server = self._config.server
            bind_dn = LoginTemplate.render(self._config.bind_dn_template, username)
            search_filter = LoginTemplate.render(self._config.user_filter, username)
            search_base = self._config.base_dn
            user_dn, samaccountname, member_of = await asyncio.to_thread(
                self._ad.fetch_userdn_samaccountname_member_of,
                server=server,
                bind_dn=bind_dn,
                bind_password=password,
                search_base=search_base,
                search_filter=search_filter,
            )

            # имя берём из каталога, а не из формы: набранный регистр на
            # роли, запреты и строку users влиять не должен
            login = UserLogin.of(samaccountname)

            if self._excluded_of(samaccountname, user_dn, member_of):
                self._logger.warning("access denied for %s (excluded)", login.key)
                raise AuthorizationError("Access denied")

            metadata: dict[str, Any] = {
                UserMetadataField.PROVIDER: SignInProvider.LDAP.value,
            }

            roles = self._roles_of(samaccountname, user_dn, member_of)

            requires_roles = self._config.require_roles
            if requires_roles and not roles:
                self._logger.warning(
                    "access denied for %s (no roles mapped)", login.key
                )
                raise AuthorizationError("Access denied")

            if roles:
                metadata[UserMetadataField.ROLES] = roles

            return SignedIn(
                identifier=login.key, display_name=login.display, metadata=metadata
            )
        except LDAPUserNotFoundError as e:
            self._logger.warning("user %s is not registered", username)
            raise AuthenticationError("User is not registered") from e
        except LDAPInvalidCredentialsError as e:
            self._logger.warning("invalid credentials for %s", username)
            raise AuthenticationError("Invalid username or password") from e
        except LDAPServerUnavailableError as e:
            self._logger.error("LDAP is unavailable", exc_info=e)
            raise ExternalServiceError(
                "ldap",
                "LDAP is unavailable, please try again later",
            ) from e
        except LDAPError as e:
            # access denied / кривой конфиг / прочее — наша вина
            self._logger.error("LDAP error: %s", e, exc_info=e)
            raise InternalServiceError(
                internal_detail=f"ldap error: {e}", user_detail=None
            ) from e


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
    def of(cls, configs: Sequence[AuthConfig]) -> CompositeSignIn | None:
        providers: list[PasswordSignIn] = []
        for config in configs:
            if isinstance(config, LocalAuthConfig):
                providers.append(LocalSignIn(config))

            if isinstance(config, LdapAuthConfig):
                providers.append(LdapSignIn(config))

        if not providers:
            return None

        return CompositeSignIn(providers)
