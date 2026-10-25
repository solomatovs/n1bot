"""Стенд интеграционных тестов: адреса, принципалы и учётки берутся из конфига.

В коде тестов остаются имена ключей, реальные хосты и учётки живут в
site.toml и stand.toml, которых нет в репозитории. Значения, которым не нужен живой
сервис (примеры доменов, разбор шаблонов), в конфиг не ходят.

Ошибки:
StandError — конфиг приложения не найден или не читается, или в секции
    [site] нет ключа стенда.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from omegaconf import OmegaConf
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from boba.identity.directory import SimpleBind
from boba.runtime.config import (
    AppLayers,
    ConfigFiles,
    ConfigLayoutError,
    ConfigLocator,
)

__all__ = ["Stand", "StandError", "StandLayers"]


class StandError(Exception):
    """Конфиг стенда недоступен или неполон."""


class StandLayers:
    """Конфиг глазами тестов: слои приложения плюс стендовый conf/stand.toml.

    В stand.toml живёт то, что нужно только тестам: адреса живых сервисов
    стенда и сервисные секции для посева ([clickhouse], confluence). Приложение
    этот файл не читает.
    """

    FILE: ClassVar[str] = "stand.toml"

    @classmethod
    def path(cls, files: ConfigFiles) -> Path:
        """Стендовый файл лежит рядом с site-файлом: оба — про машину."""
        return files.site.parent / cls.FILE

    @classmethod
    def compose(cls, files: ConfigFiles) -> Any:
        raw = AppLayers.compose(files)

        stand_path = cls.path(files)
        if not stand_path.is_file():
            return raw

        with stand_path.open("rb") as handle:
            overlay = tomllib.load(handle)

        return OmegaConf.merge(raw, OmegaConf.create(overlay))


class Stand(BaseModel):
    """Секция [site] конфига приложения глазами теста."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    SECTION: ClassVar[str] = "site"
    NETBIOS_SEPARATOR: ClassVar[str] = "\\"

    ldap_url: str = Field(
        default="",
        description=(
            "Контроллер домена стенда, ldaps://…; пусто (site сервиса без входа "
            "ldap) — тесты входа падают."
        ),
    )
    ldap_base_dn: str = ""
    ldap_auth_method: str = Field(
        default="",
        description=(
            "Выбранный служебный bind каталога; его учётка (simple) — второй "
            "пользователь стенда."
        ),
    )
    ldap_auth: Mapping[str, Mapping[str, Any]] = Field(default_factory=dict)

    krb_domain: str
    krb_realm: str
    krb_config: str = Field(description="krb5.conf стенда.")
    krb_http_keytab: str = Field(
        default="",
        description=(
            "Keytab принципала SSO-приёма; пусто (site сервиса без входа "
            "kerberos) — тесты входа падают."
        ),
    )
    pg_krb_user: str
    pg_krb_keytab: str
    ch_krb_user: str = Field(
        default="",
        description="Принципал clickhouse по keytab; пусто — тесты падают.",
    )
    ch_krb_keytab: str = ""

    developer: str = Field(
        pattern=r"^[a-z][a-z0-9_]*$",
        description=(
            "Метка разработчика (пользователь машины): по ней названы его база "
            "aid_<developer> и объекты стенда тестов на общих серверах."
        ),
    )

    pg_host: str
    pg_addr: str
    pg_port: int
    pg_database: str
    pg_krbsrvname: str
    pg_probe_user: str = Field(
        default="",
        description="Роль postgres с паролем; пусто — тест падает с причиной.",
    )
    pg_probe_password: SecretStr = SecretStr("")
    pg_probe_database: str = Field(
        default="",
        description="Тестовая база роли-пробника: в другие базы pg_hba её не пускает.",
    )

    ch_host: str
    ch_addr: str = Field(
        default="",
        description="Адрес clickhouse; пусто — тесты падают (ch живёт в базе).",
    )
    ch_port: int
    ch_database: str = ""
    ch_krbsrvname: str = ""
    ch_user: str = Field(default="", description="Пользователь clickhouse с паролем.")
    ch_password: SecretStr = SecretStr("")

    confluence_host: str
    confluence_port: int
    confluence_token: SecretStr = SecretStr("")

    @classmethod
    def load(cls) -> Stand:
        """Стенд из конфига приложения; путь берётся так же, как приложением."""
        try:
            files = ConfigLocator.files()
        except ConfigLayoutError as exc:
            msg = f"stand: locating the application config: {exc}"
            raise StandError(msg) from exc

        try:
            raw = StandLayers.compose(files)
        except Exception as exc:
            msg = f"stand: composing application config {files}: {exc}"
            raise StandError(msg) from exc

        section = OmegaConf.select(raw, cls.SECTION, throw_on_missing=True)
        if section is None:
            msg = f"stand: config {files} has no [{cls.SECTION}] section"
            raise StandError(msg)

        values = OmegaConf.to_container(section, resolve=True)
        if not isinstance(values, dict):
            got = type(values).__name__
            msg = f"stand: [{cls.SECTION}] in {files} expects a table, got {got}"
            raise StandError(msg)

        return cls._of(values)

    @classmethod
    def required(cls) -> Stand:
        """Стенд для модуля тестов; без конфига сбор модуля падает с причиной."""
        return cls.load()

    @classmethod
    def _of(cls, values: dict[Any, Any]) -> Stand:
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            msg = f"stand: validating [{cls.SECTION}] against the Stand model: {exc}"
            raise StandError(msg) from exc

    @property
    def krb_http_user(self) -> str:
        """Учётка приёма SSO: под ней приложение ходит своим keytab'ом."""
        name, _, _ = self.service_principal.partition("@")
        return name

    @property
    def service_principal(self) -> str:
        """Принципал приложения: им подписан keytab сервисных соединений."""
        return f"{self.pg_krb_user}@{self.krb_realm}"

    @property
    def reader_bind(self) -> SimpleBind:
        """Второй пользователь стенда: учётка служебного simple bind'а каталога
        [site.ldap_auth.<ldap_auth_method>], DOMAIN\\user и пароль."""
        table = self.ldap_auth.get(self.ldap_auth_method)
        if table is None:
            msg = (
                f"stand: [{self.SECTION}] has no table "
                f"ldap_auth.{self.ldap_auth_method} named by ldap_auth_method, the "
                "second stand user is its simple bind"
            )
            raise StandError(msg)

        try:
            return SimpleBind.model_validate(table)
        except ValidationError as exc:
            msg = (
                f"stand: [{self.SECTION}].ldap_auth.{self.ldap_auth_method} must be "
                f"a simple bind with user and password for the second stand user: {exc}"
            )
            raise StandError(msg) from exc

    @property
    def reader_principal(self) -> str:
        """Второй пользователь стенда в форме user@REALM."""
        _, _, name = self.reader_bind.user.rpartition(self.NETBIOS_SEPARATOR)
        return f"{name}@{self.krb_realm}"

    @property
    def reader_password(self) -> SecretStr:
        return self.reader_bind.password

    @property
    def pg_spn(self) -> str:
        """SPN postgres в форме hostbased."""
        return f"{self.pg_krbsrvname}@{self.pg_host}"

    @property
    def ch_spn(self) -> str:
        """SPN clickhouse в форме hostbased."""
        return f"{self.ch_krbsrvname}@{self.ch_host}"

    @property
    def confluence_spn(self) -> str:
        return f"HTTP@{self.confluence_host}"

    def live(self) -> bool:
        """Есть ли на машине живой KDC стенда: krb5.conf и keytab на месте."""
        if not Path(self.krb_config).is_file():
            return False

        return Path(self.krb_http_keytab).is_file()
