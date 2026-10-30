"""Секция базы ix для приложений ix и вход процесса в kerberos.

IxDatabase это общие поля секции приложения: схема графа и профиль PostgreSQL из
boba-db-postgres; секция приложения наследует модель. Соединения берутся у
AsyncPostgresPool напрямую: воркер держит одно выделенное соединение на цикл
(`AsyncPostgresPool.dedicated`), http-стенд открывает пул.

Файл конфига утилиты читает IxConfigFile одним ConfigLoader'ом. Рабочий
каталог kerberos задаёт секция [krb] того же файла, как у приложения и toolcli:
enter_kerberos ставит его один раз на процесс; секции нет — kerberos утилите не
нужен, и профили с keytab билета не получат. Дочерний процесс (источник
скрапера, спейс индексатора) получает ту же модель и ставит каталог себе сам.

Ошибки:
ConfigError — файл конфига не читается или не разрешается, секции нет или она
    не сходится с моделью.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, TypeVar

from omegaconf import OmegaConf
from pydantic import BaseModel, ConfigDict, Field

from boba.config import ConfigLoader, bind
from boba.db.postgres.connection import PostgresConfig
from boba.krb import KerberosWorkspaceConfig

__all__ = ["IxConfigFile", "IxDatabase"]

M = TypeVar("M", bound=BaseModel)


class IxDatabase(BaseModel):
    """Общие поля секции приложения ix; секция приложения наследует модель."""

    model_config = ConfigDict(extra="ignore")

    db_schema: str = Field(min_length=1)
    postgres: PostgresConfig


class IxConfigFile:
    """Файл конфига утилиты ix (--config): загружается один раз при создании,
    секции отдаёт моделями. Создаёт его вход утилиты (воркер, скрапер,
    стенд поиска) и зовёт enter_kerberos до работы с базой."""

    KERBEROS: ClassVar[str] = "krb"

    def __init__(self, path: Path) -> None:
        self._raw = ConfigLoader([path]).load()

    def section(self, path: str, model: type[M]) -> M:
        return bind(self._raw, path, model)

    def enter_kerberos(self) -> KerberosWorkspaceConfig | None:
        """Рабочий каталог kerberos из [krb]; секции нет — None, kerberos не нужен."""
        if OmegaConf.select(self._raw, self.KERBEROS) is None:
            return None

        workspace = bind(self._raw, self.KERBEROS, KerberosWorkspaceConfig)
        workspace.apply()

        return workspace
