"""Конфиги способов входа: local, ldap, kerberos, proxy; union AuthConfig по type.

Роли у каждого типа задаются секциями roles.<провайдер>: секция есть — провайдер
подключён. Провайдеры: local (таблица логинов), directory (правила по фактам
записи каталога, которую вход получил сам), ldap (поиск в каталоге под служебным
bind'ом плюс те же правила), principal (принципал и SID из PAC), header (значение
заголовка, только proxy). Профили — секциями profiles.<провайдер>: header только
у proxy, провайдер по ролям подключён у всех и настроек не имеет.
"""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from boba.identity.admission import RoleExcludeConfig, RoleMappingConfig, RoleRules
from boba.identity.session import LoginTemplate
from boba.identity.signin import ProxyHeaderNames
from boba.kerberos import AcceptConfig, Delegation

__all__ = [
    "AuthConfig",
    "DirectoryRolesConfig",
    "HeaderProfilesConfig",
    "HeaderRolesConfig",
    "KerberosAuthConfig",
    "KerberosRoleProviders",
    "LdapAuthConfig",
    "LdapRoleProviders",
    "LdapRolesConfig",
    "LocalAuthConfig",
    "LocalRoleProviders",
    "LocalRolesConfig",
    "PrincipalRolesConfig",
    "ProxyAuthConfig",
    "ProxyHeaders",
    "ProxyProfileProviders",
    "ProxyRoleProviders",
]


class RoleTables:
    """Необязательные таблицы конфига в обязательные аргументы правил."""

    @staticmethod
    def mapping(value: RoleMappingConfig | None) -> RoleMappingConfig:
        if value is None:
            return RoleMappingConfig({})

        return value

    @staticmethod
    def exclusions(value: RoleExcludeConfig | None) -> RoleExcludeConfig:
        if value is None:
            return RoleExcludeConfig([])

        return value


class DirectoryRolesConfig(BaseModel):
    """Провайдер directory: маппинги ролей и исключений по атрибутам записи
    каталога — sAMAccountName, memberOf, DN."""

    model_config = ConfigDict(extra="forbid")

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

    def rules(self) -> RoleRules:
        return RoleRules(
            require_roles=False,
            by_login=RoleTables.mapping(self.samaccountname),
            by_login_ex=RoleTables.exclusions(self.samaccountname_ex),
            by_member_of=RoleTables.mapping(self.member_of),
            by_member_of_ex=RoleTables.exclusions(self.member_of_ex),
            by_dn=RoleTables.mapping(self.dn),
            by_dn_ex=RoleTables.exclusions(self.dn_ex),
        )


class LocalRolesConfig(BaseModel):
    """Провайдер local: таблица логин → роли и логины, которым вход запрещён."""

    model_config = ConfigDict(extra="forbid")

    mapping: RoleMappingConfig = Field(default_factory=lambda: RoleMappingConfig({}))
    exclude: RoleExcludeConfig = Field(default_factory=lambda: RoleExcludeConfig([]))

    def rules(self) -> RoleRules:
        return RoleRules(
            require_roles=False, by_login=self.mapping, by_login_ex=self.exclude
        )


class PrincipalRolesConfig(BaseModel):
    """Провайдер principal: роли и исключения по принципалу и SID групп из PAC."""

    model_config = ConfigDict(extra="forbid")

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

    def rules(self) -> RoleRules:
        return RoleRules(
            require_roles=False,
            by_principal=RoleTables.mapping(self.principal),
            by_principal_ex=RoleTables.exclusions(self.principal_ex),
            by_sid=RoleTables.mapping(self.sid),
            by_sid_ex=RoleTables.exclusions(self.sid_ex),
        )


class LdapRolesConfig(BaseModel):
    """Провайдер ldap: поиск пользователя в каталоге под служебным bind'ом и
    правила по найденной записи. Для входов без пароля пользователя — kerberos
    и proxy."""

    model_config = ConfigDict(extra="forbid")

    server: str = Field(
        description="URI контроллера домена, напр. ldaps://dc.corp.example.com:636.",
    )
    base_dn: str = Field(
        description="База поиска пользователя, напр. DC=corp,DC=example,DC=com.",
    )
    bind_dn: str
    bind_password: SecretStr
    mapping: DirectoryRolesConfig = Field(default=DirectoryRolesConfig())


class HeaderRolesConfig(BaseModel):
    """Провайдер header: роли через запятую в заголовке доверенного бэкенда."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="X-Remote-Roles", min_length=1)


class HeaderProfilesConfig(BaseModel):
    """Провайдер профилей header: имена профилей через запятую в заголовке name
    и, если задан selected, выбранный для новых чатов профиль в отдельном
    заголовке."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="X-Remote-Profiles", min_length=1)
    selected: str = Field(
        default="",
        description="Заголовок выбранного профиля; пусто — выбор не читается.",
    )


class LocalRoleProviders(BaseModel):
    """Провайдеры ролей local-входа."""

    model_config = ConfigDict(extra="forbid")

    local: LocalRolesConfig | None = None


class LdapRoleProviders(BaseModel):
    """Провайдеры ролей ldap-входа: запись каталога уже получена bind'ом."""

    model_config = ConfigDict(extra="forbid")

    directory: DirectoryRolesConfig | None = None


class KerberosRoleProviders(BaseModel):
    """Провайдеры ролей kerberos-входа."""

    model_config = ConfigDict(extra="forbid")

    principal: PrincipalRolesConfig | None = None
    ldap: LdapRolesConfig | None = None


class ProxyRoleProviders(BaseModel):
    """Провайдеры ролей proxy-входа."""

    model_config = ConfigDict(extra="forbid")

    local: LocalRolesConfig | None = None
    ldap: LdapRolesConfig | None = None
    header: HeaderRolesConfig | None = None


class ProxyProfileProviders(BaseModel):
    """Провайдеры профилей proxy-входа сверх провайдера по ролям."""

    model_config = ConfigDict(extra="forbid")

    header: HeaderProfilesConfig | None = None


class LocalAuthConfig(BaseModel):
    """Авторизация по статической таблице логин/пароль из конфига."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["local"] = "local"

    users: dict[str, str] = Field(
        default_factory=dict,
        description="Таблица логин→пароль; роли — провайдерами roles.*.",
    )
    roles: LocalRoleProviders = Field(default=LocalRoleProviders())
    require_roles: bool = Field(
        default=True,
        description=(
            "403 после успешной аутентификации, "
            "если пользователю не замапилась ни одна роль."
        ),
    )


class LdapAuthConfig(BaseModel):
    """Логин/пароль с проверкой bind'ом в AD; роли — провайдером directory."""

    model_config = ConfigDict(extra="forbid")

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

    roles: LdapRoleProviders = Field(default=LdapRoleProviders())
    require_roles: bool = Field(
        default=True,
        description=(
            "403 после успешной аутентификации, "
            "если пользователю не замапилась ни одна роль."
        ),
    )


class KerberosAuthConfig(BaseModel):
    """SSO через Kerberos/SPNEGO: тикет валидирует middleware, роли — провайдерами
    principal и ldap."""

    model_config = ConfigDict(extra="forbid")

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
    roles: KerberosRoleProviders = Field(default=KerberosRoleProviders())
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


class ProxyHeaders(BaseModel):
    """Имена заголовков proxy-входа: логин, метка времени и подпись. Заголовки
    ролей и профилей задают их провайдеры."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user: str = Field(default="X-Remote-User", min_length=1)
    timestamp: str = Field(default="X-Boba-Timestamp", min_length=1)
    signature: str = Field(default="X-Boba-Signature", min_length=1)


class ProxyAuthConfig(BaseModel):
    """Вход по доверенному заголовку: логин ставит свой бэкенд и подписывает
    запрос общим секретом; роли и профили — провайдерами roles.* и profiles.*."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["proxy"] = "proxy"

    path: str = Field(default="/auth/proxy")
    secret: SecretStr = Field(
        description="Ключ HMAC-SHA256 подписи login:timestamp:roles; не auth_secret.",
    )
    headers: ProxyHeaders = Field(default=ProxyHeaders())
    max_skew_sec: int = Field(
        default=60,
        gt=0,
        description="Допустимая разница между меткой времени запроса и часами сервера.",
    )
    allowed_clients: list[str] = Field(
        default_factory=list,
        description="Сети CIDR, откуда принимается вход; пусто — без фильтра.",
    )
    roles: ProxyRoleProviders = Field(default=ProxyRoleProviders())
    profiles: ProxyProfileProviders = Field(default=ProxyProfileProviders())
    require_roles: bool = Field(
        default=True,
        description="403, если ни один провайдер не дал роли.",
    )

    @field_validator("secret")
    @classmethod
    def _secret_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            msg = "[auth.proxy].secret expects a non-empty signing key"
            raise ValueError(msg)

        return value

    @field_validator("allowed_clients")
    @classmethod
    def _networks_parse(cls, value: list[str]) -> list[str]:
        for raw in value:
            try:
                ip_network(raw, strict=False)
            except ValueError as exc:
                msg = (
                    f"[auth.proxy].allowed_clients: {raw!r} is not a CIDR "
                    f"network: {exc}"
                )
                raise ValueError(msg) from exc

        return value

    def header_names(self) -> ProxyHeaderNames:
        """Имена заголовков для транспорта: базовые плюс те, что подключили
        провайдеры ролей и профилей."""
        roles = ""
        if self.roles.header is not None:
            roles = self.roles.header.name

        profiles = ""
        profile = ""
        if self.profiles.header is not None:
            profiles = self.profiles.header.name
            profile = self.profiles.header.selected

        return ProxyHeaderNames(
            user=self.headers.user,
            timestamp=self.headers.timestamp,
            signature=self.headers.signature,
            roles=roles,
            profiles=profiles,
            profile=profile,
        )

    def networks(self) -> list[IPv4Network | IPv6Network]:
        parsed: list[IPv4Network | IPv6Network] = []
        for raw in self.allowed_clients:
            parsed.append(ip_network(raw, strict=False))

        return parsed


AuthConfig = Annotated[
    LocalAuthConfig | KerberosAuthConfig | LdapAuthConfig | ProxyAuthConfig,
    Field(discriminator="type"),
]
