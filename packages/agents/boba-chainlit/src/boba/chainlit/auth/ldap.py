import asyncio
import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

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
from pydantic import BaseModel, Field, field_validator

import chainlit as cl
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
    LdapRolesConfig,
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


class LdapCredentialConfig(BaseModel):
    bind_dn: str = Field(description="DN сервисной учётки для поиска пользователя.")
    bind_password: str = Field(description="Пароль сервисной учётки (секрет).")


class LdapAuthConfig(BaseModel):
    """Логин/пароль с проверкой bind'ом в AD; роль — из групп AD (как kerberos)."""

    type: Literal["ldap"] = "ldap"
    server: str = Field(
        description="URI контроллера домена, напр. ldaps://dc.corp.example.com:636.",
    )
    base_dn: str = Field(
        description="База поиска пользователя, напр. DC=corp,DC=example,DC=com.",
    )
    user_filter: str = Field(
        default="(sAMAccountName={username})",
        description="LDAP-фильтр поиска пользователя; {username} подставляется.",
    )
    bind_dn_template: str = Field(
        description="LDAP bind user; {username} подставляется",
    )

    @field_validator("user_filter", "bind_dn_template")
    @classmethod
    def _template_has_username(cls, value: str) -> str:
        return LoginTemplate.check(value)

    roles: LdapRolesConfig = Field(
        default=LdapRolesConfig(),
        description="Мапперы учеток и ролей",
    )
    require_roles: bool = Field(
        default=True,
        description=(
            "403 после успешной аутентификации, "
            "если пользователю не замапилась ни одна роль."
        ),
    )


class LdapAuth:
    "Логин/пароль с проверкой bind'ом в AD; роль забираем из групп AD"

    def __init__(self, config: LdapAuthConfig):
        self._provider = "ldap"
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

    async def password_auth(self, username: str, password: str) -> cl.User | None:
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

            return cl.User(
                identifier=login.key,
                display_name=login.display,
                metadata=metadata,
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
