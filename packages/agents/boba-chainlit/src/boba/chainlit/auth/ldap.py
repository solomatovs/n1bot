import asyncio
import logging
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import chain
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
from pydantic import BaseModel, Field

import chainlit as cl
from boba.chainlit.auth.errors import (
    AuthenticationError,
    AuthorizationError,
    ExternalServiceError,
    InternalServiceError,
)
from boba.chainlit.auth.local import (
    LocalExcludeUserProvider,
    LocalUserRolesProvider,
    RoleExcludeConfig,
    RoleMappingConfig,
)
from boba.chainlit.infra.session import UserMetadataField


class LDAPError(Exception):
    "База ошибок каталога; транспортно-нейтральна, домен мапят вызывающие."


class LDAPServerUnavailableError(LDAPError):
    "Каталог недоступен (сокет/сеть/TLS/таймаут) — не наша вина."


class LDAPInvalidCredentialsError(LDAPError):
    "bind отклонён: неверные креды (юзер или сервис-аккаунт — решает вызывающий)."


class LDAPAccessDeniedError(LDAPError):
    "Недостаточно прав на операцию (insufficient access)."


class LDAPConfigError(LDAPError):
    "Кривой конфиг: несуществующий base DN, неверный DN/фильтр/сервер/TLS-политика."


class LDAPUserNotFoundError(LDAPError):
    "Поиск выполнен, но запись пользователя не найдена."


@dataclass(frozen=True)
class ADUserEntry:
    """Атрибуты пользователя из AD для маппинга ролей/исключений."""

    dn: str
    samaccountname: str
    member_of: list[str]


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


class LdapRolesConfig(BaseModel):
    samaccountname: RoleMappingConfig | None = Field(
        default=None,
        description="",
    )
    samaccountname_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="Логины, которым запрещён вход (403).",
    )
    member_of: RoleMappingConfig | None = Field(
        default=None,
        description="",
    )
    member_of_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="Группы, членам которых запрещён вход (403).",
    )
    dn: RoleMappingConfig | None = Field(
        default=None,
        description="",
    )
    dn_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="DN пользователей, которым запрещён вход (403).",
    )


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


class SAMAccountNameUserRolesProvider:
    """Мапер sAMAccountName - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, samaccountname: str) -> Iterable[str]:
        yield from self._mapping.roles_of(samaccountname)


class SAMAccountNameExcludeUserProvider:
    """Список sAMAccountName, которым запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, samaccountname: str) -> Iterable[bool]:
        yield from self._mapping.exclude_of(samaccountname)


class MemberOfUserRolesProvider:
    """Мапер групп memberOf - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, member_of: list[str]) -> Iterable[str]:
        for m in member_of:
            yield from self._mapping.roles_of(m)


class MemberOfExcludeUserProvider:
    """Список групп memberOf, членам которых запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, member_of: list[str]) -> Iterable[bool]:
        for m in member_of:
            yield from self._mapping.exclude_of(m)


class DnUserRolesProvider:
    """Мапер DN пользователя - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, dn: str) -> Iterable[str]:
        return self._mapping.roles_of(dn)


class DnExcludeUserProvider:
    """Список DN пользователей, которым запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, dn: str) -> Iterable[bool]:
        yield from self._mapping.exclude_of(dn)


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
        res = []
        if self._samaccountname_roles_ex:
            res.append(self._samaccountname_roles_ex.exclude_of(username))

        if self._member_of_roles_ex:
            res.append(self._member_of_roles_ex.exclude_of(member_of))

        if self._dn_roles_ex:
            res.append(self._dn_roles_ex.exclude_of(user_dn))

        return any(chain.from_iterable(res))

    def _roles_of(self, username: str, user_dn: str, member_of: list[str]) -> list[str]:
        roles: list[str] = []
        if self._samaccountname_roles:
            roles.extend(self._samaccountname_roles.roles_of(username))

        if self._member_of_roles:
            roles.extend(self._member_of_roles.roles_of(member_of))

        if self._dn_roles:
            roles.extend(self._dn_roles.roles_of(user_dn))

        return list(set(roles))

    async def password_auth(self, username: str, password: str) -> cl.User | None:
        # личность подтверждаем bind'ом под пользователем
        try:
            server = self._config.server
            bind_dn = self._config.bind_dn_template.format(username=username)
            search_filter = self._config.user_filter.format(username=username)
            search_base = self._config.base_dn
            user_dn, member_of = await asyncio.to_thread(
                self._ad.fetch_userdn_and_member_of,
                server=server,
                bind_dn=bind_dn,
                bind_password=password,
                search_base=search_base,
                search_filter=search_filter,
            )

            if self._excluded_of(username, user_dn, member_of):
                self._logger.warning("access denied for %s (excluded)", username)
                raise AuthorizationError("Access denied")

            metadata: dict[str, Any] = {
                UserMetadataField.PROVIDER: LdapAuth.__name__,
            }

            roles = self._roles_of(username, user_dn, member_of)

            if self._config.require_roles and not roles:
                self._logger.warning(
                    "access denied for %s (no roles mapped)", username
                )
                raise AuthorizationError("Access denied")

            if roles:
                metadata[UserMetadataField.ROLES] = roles

            return cl.User(
                identifier=username,
                display_name=username,
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
