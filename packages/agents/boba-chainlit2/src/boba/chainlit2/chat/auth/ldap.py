import asyncio
import logging
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from typing import Any, Literal

import chainlit as cl
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

from boba.chainlit2.chat.auth.fix import FixUserRolesProvider, RolesMappingConfig
from boba.chainlit2.errors import (
    AuthenticationError,
    ExternalServiceError,
    InternalServiceError,
)


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
                # без этого ошибки search'а (нет base DN, нет прав) молча дают
                # пустой результат и выглядят как "пользователь не найден"
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
    fix: RolesMappingConfig | None = Field(
        default=None,
        description="",
    )
    member_of: RolesMappingConfig | None = Field(
        default=None,
        description="",
    )
    dn: RolesMappingConfig | None = Field(
        default=None,
        description="",
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


class MemberOfUserRolesProvider:
    """Фиксированный провайдер пользователь - список ролей"""

    def __init__(self, mapping: RolesMappingConfig):
        self._mapping = mapping

    def roles_of(self, member_of: list[str]) -> Iterable[str]:
        for m in member_of:
            yield from self._mapping.roles_of(m)


class DnUserRolesProvider:
    """Фиксированный провайдер пользователь - список ролей"""

    def __init__(self, mapping: RolesMappingConfig):
        self._mapping = mapping

    def roles_of(self, dn: str) -> Iterable[str]:
        return self._mapping.roles_of(dn)


class LdapAuth:
    """
    Логин/пароль с проверкой bind'ом в AD
    роль забираем из групп AD
    """

    def __init__(self, config: LdapAuthConfig):
        self._provider = "ldap"
        self._config = config
        self._ad = ADDirectory
        self._logger = logging.getLogger(__name__)

        self._init_mapping()

    def _init_mapping(self):
        self._fixed_roles: FixUserRolesProvider | None = None
        self._member_of_roles: MemberOfUserRolesProvider | None = None
        self._dn_roles: DnUserRolesProvider | None = None

        if roles := self._config.roles.fix:
            self._fixed_roles = FixUserRolesProvider(roles)

        if roles := self._config.roles.member_of:
            self._member_of_roles = MemberOfUserRolesProvider(roles)

        if roles := self._config.roles.dn:
            self._dn_roles = DnUserRolesProvider(roles)

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

            metadata: dict[str, Any] = {"provider": LdapAuth.__name__}

            roles: list[str] = []
            if self._fixed_roles:
                roles.extend(self._fixed_roles.roles_of(username))

            if self._member_of_roles:
                roles.extend(self._member_of_roles.roles_of(member_of))

            if self._dn_roles:
                roles.extend(self._dn_roles.roles_of(user_dn))

            roles = list(set(roles))

            if roles:
                metadata.update(roles=roles)

            return cl.User(
                identifier=username,
                display_name=username,
                metadata=metadata,
            )
        except LDAPUserNotFoundError as e:
            self._logger.warning("user %s is not registered", username)
            raise AuthenticationError(
                "User is not registered"
            ) from e
        except LDAPInvalidCredentialsError as e:
            self._logger.warning("invalid credentials for %s", username)
            raise AuthenticationError(
                "Invalid username or password"
            ) from e
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
