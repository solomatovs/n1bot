"""Стенд интеграционных тестов: адреса, принципалы и учётки берутся из конфига.

В коде тестов остаются имена ключей, реальные хосты и учётки живут в секции
[stand] файла-наложения стенда (stand.toml разработчика), которого нет в
репозитории. Файлы называют переменные прогона (StandConfigEnv): у чата —
BOBA_CONFIG_PATH, у сервиса boba-mcp — BOBA_MCP_CONFIG_PATH; в каждой через
os.pathsep сначала файлы приложения (главный конфиг и наложения), последним —
наложение стенда. Значения, которым не нужен живой сервис (примеры доменов,
разбор шаблонов), в конфиг не ходят.

Ошибки:
StandError — переменная файлов конфига не задана, в ней нет наложения стенда,
    конфиг не загружается или в секции [stand] нет ключа стенда.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from boba.config import ConfigError, ConfigLoader, bind
from boba.identity.directory import SimpleBind

__all__ = ["Stand", "StandConfigEnv", "StandConfigPaths", "StandError"]


class StandError(Exception):
    """Конфиг стенда недоступен или неполон."""


class StandConfigEnv(StrEnum):
    """Переменные прогона с файлами конфига приложений стенда: через
    os.pathsep файлы приложения (главный конфиг, затем наложения), последним —
    наложение стенда stand.toml."""

    RUN = "BOBA_CONFIG_PATH"
    """Чат boba-aid: конфиг прогона фикстур и стенда чата."""
    SERVICE = "BOBA_MCP_CONFIG_PATH"
    """Сервис boba-mcp, который исполняет инструменты."""


class StandConfigPaths:
    """Файлы конфига приложения стенда из переменной прогона env.

    Приложения получают файлы аргументами --config; переменная — граница
    конфига тестов, у которых аргументов нет (фикстуры, Stand). Все файлы
    (paths) читают тесты; процессу приложения стенд отдаёт файлы без
    наложения стенда (app_paths), главный файл (main) называет каталог
    conf/ экземпляра, где лежит krb/.
    """

    LEAST: ClassVar[int] = 2
    """Файл приложения и наложение стенда."""

    def __init__(self, env: StandConfigEnv) -> None:
        self._env = env

    def paths(self) -> tuple[Path, ...]:
        value = os.environ.get(self._env.value)
        if not value:
            msg = (
                f"stand: {self._env.value} is required in the environment: the "
                "config files of the app and the stand overlay last, separated "
                "by os.pathsep, got nothing"
            )
            raise StandError(msg)

        paths: list[Path] = []
        for part in value.split(os.pathsep):
            if not part:
                continue

            paths.append(Path(part))

        if len(paths) < self.LEAST:
            msg = (
                f"stand: {self._env.value} must name the config of the app and "
                f"the stand overlay stand.toml last, got {len(paths)} file(s): "
                f"{value}"
            )
            raise StandError(msg)

        return tuple(paths)

    def app_paths(self) -> tuple[Path, ...]:
        """Файлы приложения без наложения стенда: их получает процесс."""
        return self.paths()[:-1]

    def main(self) -> Path:
        """Главный файл приложения: рядом с ним conf/krb экземпляра."""
        return self.paths()[0]

    def load(self) -> DictConfig:
        paths = self.paths()
        try:
            return ConfigLoader(paths).load()
        except ConfigError as exc:
            msg = f"stand: loading the config files of {self._env.value}: {exc}"
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
        config = StandConfigPaths(StandConfigEnv.RUN)
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
