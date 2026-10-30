"""Стенд интеграционных тестов: адреса, принципалы и учётки берутся из конфига.

В коде тестов остаются имена ключей, реальные хосты и учётки живут в секции
[stand] файла-наложения стенда (stand.toml рядом с config.toml отладки), которого
нет в репозитории. Файлы называет переменная BOBA_CONFIG_PATH прогона: главный
конфиг и наложения через os.pathsep. Значения, которым не нужен живой сервис
(примеры доменов, разбор шаблонов), в конфиг не ходят.

Ошибки:
StandError — BOBA_CONFIG_PATH не задана, конфиг не загружается или в секции
    [stand] нет ключа стенда.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from boba.config import ConfigError, ConfigLoader, bind
from boba.identity.directory import SimpleBind

__all__ = ["Stand", "StandConfigPaths", "StandError"]


class StandError(Exception):
    """Конфиг стенда недоступен или неполон."""


class StandConfigPaths:
    """Файлы конфига прогона: BOBA_CONFIG_PATH — главный конфиг и наложения
    через os.pathsep. Приложения получают файлы аргументами --config; эта
    переменная — граница конфига тестов, у которых аргументов нет (фикстуры,
    Stand)."""

    ENV: ClassVar[str] = "BOBA_CONFIG_PATH"

    def paths(self) -> tuple[Path, ...]:
        value = os.environ.get(self.ENV)
        if not value:
            msg = (
                f"stand: {self.ENV} is required in the environment: the config.toml "
                "of the run and its overlays separated by os.pathsep, got nothing"
            )
            raise StandError(msg)

        paths: list[Path] = []
        for part in value.split(os.pathsep):
            if not part:
                continue

            paths.append(Path(part))

        return tuple(paths)

    def load(self) -> DictConfig:
        paths = self.paths()
        try:
            return ConfigLoader(paths).load()
        except ConfigError as exc:
            msg = f"stand: loading the config of the run {self.ENV}: {exc}"
            raise StandError(msg) from exc


class Stand(BaseModel):
    """Секция [stand] наложения стенда глазами теста."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    SECTION: ClassVar[str] = "stand"
    NETBIOS_SEPARATOR: ClassVar[str] = "\\"

    ldap_url: str = Field(
        default="",
        description=(
            "Контроллер домена стенда, ldaps://…; пусто (стенд без входа "
            "ldap) — тесты входа падают."
        ),
    )
    ldap_base_dn: str = ""
    reader: SimpleBind = Field(
        description=(
            "Служебный simple bind каталога стенда: его учётка — второй "
            "пользователь стенда."
        ),
    )

    krb_domain: str
    krb_realm: str
    krb_config: str = Field(description="krb5.conf стенда.")
    krb_http_keytab: str = Field(
        default="",
        description=(
            "Keytab принципала SSO-приёма; пусто (стенд без входа "
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
        """Стенд из конфига прогона: секция [stand] файлов BOBA_CONFIG_PATH."""
        config = StandConfigPaths()
        raw = config.load()
        try:
            return bind(raw, cls.SECTION, cls)
        except ConfigError as exc:
            msg = f"stand: [{cls.SECTION}] of {config.paths()}: {exc}"
            raise StandError(msg) from exc

    @classmethod
    def required(cls) -> Stand:
        """Стенд для модуля тестов; без конфига сбор модуля падает с причиной."""
        return cls.load()

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
        [stand].reader, DOMAIN\\user и пароль."""
        return self.reader

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
