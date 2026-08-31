"""Стенд интеграционных тестов: адреса, принципалы и учётки берутся из конфига.

В коде тестов остаются имена ключей, реальные хосты и учётки живут в
config.toml, которого нет в репозитории. Значения, которым не нужен живой
сервис (примеры доменов, разбор шаблонов), в конфиг не ходят.

Ошибки:
StandError — конфиг приложения не читается или в секции [site] нет ключа
    стенда.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

import pytest
from omegaconf import OmegaConf
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from boba.runtime.config import AppLayers, ConfigLocator

__all__ = ["Stand", "StandError"]


class StandError(Exception):
    """Конфиг стенда недоступен или неполон."""


class Stand(BaseModel):
    """Секция [site] конфига приложения глазами теста."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    SECTION: ClassVar[str] = "site"
    NETBIOS_SEPARATOR: ClassVar[str] = "\\"

    ldap_url: str = Field(description="Контроллер домена стенда, ldaps://…")
    ldap_base_dn: str
    ldap_bind_user: str = Field(description="Второй пользователь стенда, DOMAIN\\user.")
    ldap_bind_password: SecretStr

    krb_domain: str
    krb_realm: str
    krb_config: str = Field(description="krb5.conf стенда.")
    krb_http_keytab: str = Field(description="Keytab принципала SSO-приёма.")
    krb_pg_user: str
    krb_pg_keytab: str
    krb_ch_user: str = Field(
        default="",
        description="Принципал clickhouse по keytab; пусто — тесты пропускаются.",
    )
    krb_ch_keytab: str = ""

    pg_host: str
    pg_addr: str
    pg_port: int
    pg_database: str
    pg_krbsrvname: str
    pg_probe_user: str = Field(
        default="", description="Роль postgres с паролем; пусто — тест пропускается."
    )
    pg_probe_password: SecretStr = SecretStr("")

    ch_host: str
    ch_addr: str = Field(
        default="",
        description="Адрес clickhouse; пусто — тесты пропускаются (ch живёт в базе).",
    )
    ch_port: int
    ch_database: str = ""
    ch_krbsrvname: str = ""
    ch_user: str = Field(default="", description="Пользователь clickhouse с паролем.")
    ch_password: SecretStr = SecretStr("")

    confluence_url: str
    confluence_token: SecretStr = SecretStr("")

    @classmethod
    def load(cls) -> Stand:
        """Стенд из конфига приложения; путь берётся так же, как приложением."""
        try:
            raw = AppLayers.compose(ConfigLocator.path())
        except Exception as exc:
            msg = f"stand: application config is unavailable: {exc}"
            raise StandError(msg) from exc

        section = OmegaConf.select(raw, cls.SECTION, throw_on_missing=True)
        if section is None:
            msg = f"stand: config has no [{cls.SECTION}] section"
            raise StandError(msg)

        values = OmegaConf.to_container(section, resolve=True)
        if not isinstance(values, dict):
            msg = f"stand: [{cls.SECTION}] is not a table"
            raise StandError(msg)

        return cls._of(values)

    @classmethod
    def required(cls) -> Stand:
        """Стенд для модуля тестов; без конфига модуль пропускается целиком."""
        try:
            return cls.load()
        except StandError as exc:
            pytest.skip(str(exc), allow_module_level=True)

    @classmethod
    def _of(cls, values: dict[Any, Any]) -> Stand:
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            msg = f"stand: [{cls.SECTION}] misses stand keys: {exc}"
            raise StandError(msg) from exc

    @property
    def krb_http_user(self) -> str:
        """Учётка приёма SSO: под ней приложение ходит своим keytab'ом."""
        name, _, _ = self.service_principal.partition("@")
        return name

    @property
    def service_principal(self) -> str:
        """Принципал приложения: им подписан keytab сервисных соединений."""
        return f"{self.krb_pg_user}@{self.krb_realm}"

    @property
    def reader_principal(self) -> str:
        """Второй пользователь стенда: DOMAIN\\user из ldap-bind в форме user@REALM."""
        _, _, name = self.ldap_bind_user.rpartition(self.NETBIOS_SEPARATOR)
        return f"{name}@{self.krb_realm}"

    @property
    def reader_password(self) -> SecretStr:
        return self.ldap_bind_password

    @property
    def pg_spn(self) -> str:
        """SPN postgres в форме hostbased."""
        return f"{self.pg_krbsrvname}@{self.pg_host}"

    @property
    def ch_spn(self) -> str:
        """SPN clickhouse в форме hostbased."""
        return f"{self.ch_krbsrvname}@{self.ch_host}"

    @property
    def confluence_host(self) -> str:
        host = urlparse(self.confluence_url).hostname
        if host is None:
            msg = f"stand: confluence_url has no host: {self.confluence_url!r}"
            raise StandError(msg)

        return host

    @property
    def confluence_spn(self) -> str:
        return f"HTTP@{self.confluence_host}"

    def krb_dir(self) -> Path:
        """Каталог krb5.conf: рядом лежат keytab'ы стенда."""
        return Path(self.krb_config).parent

    def live(self) -> bool:
        """Есть ли на машине живой KDC стенда: krb5.conf и keytab на месте."""
        if not Path(self.krb_config).is_file():
            return False

        return Path(self.krb_http_keytab).is_file()
