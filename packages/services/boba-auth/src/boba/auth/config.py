"""Конфиги способов входа: local, ldap, kerberos; union AuthConfig по полю type."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from boba.identity.admission import RoleExcludeConfig, RoleMappingConfig, RoleRules
from boba.identity.session import LoginTemplate
from boba.kerberos import AcceptConfig, Delegation

__all__ = [
    "AuthConfig",
    "KerberosAuthConfig",
    "KerberosRolesConfig",
    "KerberosRolesInLdapConfig",
    "KerberosRolesInLdapMappingConfig",
    "LdapAuthConfig",
    "LocalAuthConfig",
]


class LdapRolesConfig(BaseModel):
    """Маппинги ролей и исключений по атрибутам каталога."""

    samaccountname: RoleMappingConfig | None = Field(default=None, description="")
    samaccountname_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="Логины, которым запрещён вход (403).",
    )
    member_of: RoleMappingConfig | None = Field(default=None, description="")
    member_of_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="Группы, членам которых запрещён вход (403).",
    )
    dn: RoleMappingConfig | None = Field(default=None, description="")
    dn_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="DN пользователей, которым запрещён вход (403).",
    )

    def rules(self, require_roles: bool) -> RoleRules:
        return RoleRules(
            require_roles=require_roles,
            by_login=_mapping(self.samaccountname),
            by_login_ex=_exclusions(self.samaccountname_ex),
            by_member_of=_mapping(self.member_of),
            by_member_of_ex=_exclusions(self.member_of_ex),
            by_dn=_mapping(self.dn),
            by_dn_ex=_exclusions(self.dn_ex),
        )


def _mapping(value: RoleMappingConfig | None) -> RoleMappingConfig:
    if value is None:
        return RoleMappingConfig({})

    return value


def _exclusions(value: RoleExcludeConfig | None) -> RoleExcludeConfig:
    if value is None:
        return RoleExcludeConfig([])

    return value


class LocalAuthConfig(BaseModel):
    """Авторизация по статической таблице логин/пароль из конфига."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["local"] = "local"

    users: dict[str, str] = Field(
        default_factory=dict,
        description="Таблица логин→пароль; роли — из roles/roles_ex по логину.",
    )

    roles: RoleMappingConfig | None = Field(
        default=None,
        description="Источник ролей для пользователей",
    )

    roles_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="Список логинов, которым запрещён вход (403).",
    )

    require_roles: bool = Field(
        default=True,
        description=(
            "403 после успешной аутентификации, "
            "если пользователю не замапилась ни одна роль."
        ),
    )

    def rules(self) -> RoleRules:
        return RoleRules(
            require_roles=self.require_roles,
            by_login=_mapping(self.roles),
            by_login_ex=_exclusions(self.roles_ex),
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

    def rules(self) -> RoleRules:
        return self.roles.rules(self.require_roles)


class KerberosRolesInLdapMappingConfig(LdapRolesConfig):
    """Мапинг ролей/исключений по атрибутам AD; поля наследуются от LdapRolesConfig."""


class KerberosRolesInLdapConfig(BaseModel):
    server: str = Field(
        description="URI контроллера домена, напр. ldaps://dc.corp.example.com:636.",
    )
    base_dn: str = Field(
        description="База поиска пользователя, напр. DC=corp,DC=example,DC=com.",
    )
    bind_dn: str
    bind_password: SecretStr
    mapping: KerberosRolesInLdapMappingConfig = Field(
        default=KerberosRolesInLdapMappingConfig(),
    )


class KerberosRolesConfig(BaseModel):
    principal: RoleMappingConfig | None = None
    principal_ex: RoleExcludeConfig | None = None
    sid: RoleMappingConfig | None = Field(
        default=None,
        description="Мапер SID группы из PAC kerberos-тикета - роли.",
    )
    sid_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="SID групп из PAC, членам которых запрещён вход (403).",
    )

    def rules(self, require_roles: bool) -> RoleRules:
        return RoleRules(
            require_roles=require_roles,
            by_principal=_mapping(self.principal),
            by_principal_ex=_exclusions(self.principal_ex),
            by_sid=_mapping(self.sid),
            by_sid_ex=_exclusions(self.sid_ex),
        )


class KerberosAuthConfig(BaseModel):
    """SSO через Kerberos/SPNEGO: тикет валидирует middleware, роль — из групп AD."""

    type: Literal["kerberos"] = "kerberos"

    accept: AcceptConfig = Field(
        description=(
            "SPN и keytab сервиса для SPNEGO-accept; "
            "в конфиге подключается ссылкой ${kerberos.<name>}."
        ),
    )
    principal_format: str
    sso_path: str = Field(default="/auth/sso")
    delegation: Delegation = Field(
        description=(
            "Режим делегирования: forwarded (неограниченное, TGT от браузера) "
            "или constrained (S4U2Proxy по msDS-AllowedToDelegateTo)."
        ),
    )
    roles: KerberosRolesConfig | None = None
    ldap_roles: KerberosRolesInLdapConfig | None = None
    require_roles: bool = Field(
        default=True,
        description=(
            "403 после успешной аутентификации, "
            "если пользователю не замапилась ни одна роль."
        ),
    )

    @field_validator("principal_format")
    @classmethod
    def _principal_format_has_username(cls, value: str) -> str:
        return LoginTemplate.check_principal(value)

    def rules(self) -> RoleRules:
        """Правила по принципалу и SID плюс маппинги каталога, если он настроен."""
        rules = RoleRules(require_roles=self.require_roles)
        if self.roles is not None:
            rules = rules.merged(self.roles.rules(self.require_roles))

        if self.ldap_roles is not None:
            rules = rules.merged(self.ldap_roles.mapping.rules(self.require_roles))

        return rules


AuthConfig = Annotated[
    LocalAuthConfig | KerberosAuthConfig | LdapAuthConfig,
    Field(discriminator="type"),
]
