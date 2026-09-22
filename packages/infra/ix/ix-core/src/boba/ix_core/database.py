"""Секция базы ix для приложений ix и вход процесса в kerberos.

IxDatabase это общие поля секции приложения: схема графа и профиль PostgreSQL из
boba-db-postgres; секция приложения наследует модель. Соединения берутся у
AsyncPostgresPool напрямую: воркер держит одно выделенное соединение на цикл
(`AsyncPostgresPool.dedicated`), http-стенд открывает пул.

Рабочий каталог kerberos задаёт секция [krb] того же файла конфига, как у
приложения и toolcli: enter_kerberos ставит его один раз на процесс, и без секции
профили с keytab не смогут получить билет. Дочерний процесс (источник скрапера,
спейс индексатора) получает ту же модель и ставит каталог себе сам.

Ошибки:
ConfigError — файл конфига не читается или секция [krb] не сходится с моделью.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from boba.config import bind_optional_section
from boba.db.postgres.profile import PostgresConfig
from boba.krb import KerberosWorkspaceConfig

__all__ = ["IxDatabase", "enter_kerberos"]


class IxDatabase(BaseModel):
    """Общие поля секции приложения ix; секция приложения наследует модель."""

    model_config = ConfigDict(extra="ignore")

    db_schema: str = Field(min_length=1)
    postgres: PostgresConfig


def enter_kerberos(config_path: Path) -> KerberosWorkspaceConfig | None:
    """Рабочий каталог kerberos из [krb] файла конфига; нет секции — None."""
    workspace = bind_optional_section(config_path, "krb", KerberosWorkspaceConfig)
    if workspace is not None:
        workspace.apply()

    return workspace
