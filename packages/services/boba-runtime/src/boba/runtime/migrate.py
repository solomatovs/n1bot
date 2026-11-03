"""Команда migrate: конфиг приложения, планы плагинов и явный DDL PostgreSQL.

Ошибки:
ConfigError — секции конфига или план плагина не прошли проверку.
PostgresError — схема не пуста либо БД отклонила миграцию.
RuntimeError — программа плагина не отдала план создания хранилища.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict, Field

from boba.config import ConfigSchema, bind
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.migrate import (
    MigrationScripts,
    MigrationTarget,
    PostgresMigrations,
)
from boba.runtime.config import PathsConfig, RawConfig, TlsConfig


class MigrationLocation(BaseModel):
    """Только подключение и схема из конфига; параметры сервера для DDL не нужны."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    postgres: PostgresConfig
    db_schema: str = Field(alias="schema", min_length=1)


class MigrationSources(Protocol):
    """Дополнительные планы инфраструктуры, подключаемые корнем приложения."""

    async def targets(self, raw: DictConfig) -> Sequence[MigrationTarget]: ...


class MigrationConfig(MigrationLocation):
    """Минимальные секции команды migrate, включая окружение Kerberos."""

    paths: PathsConfig
    tls: TlsConfig = Field(default_factory=TlsConfig)


class ApplicationMigrations:
    """Собирает планы по конфигу и создаёт пустые схемы без запуска приложения.

    Вызывается подкомандой migrate AID, MCP или Boba. Плагины только отдают
    SQL и ссылку на подключение; учётные данные остаются у команды приложения.
    """

    def __init__(
        self,
        sources: Sequence[MigrationSources] = (),
        *,
        schema: ConfigSchema | None = None,
    ) -> None:
        self._migrations = PostgresMigrations()
        self._sources = tuple(sources)
        self._schema = schema or ConfigSchema(MigrationConfig)

    async def run(
        self,
        paths: Sequence[Path],
        packages: Sequence[str],
        *,
        section: str = "app",
    ) -> None:
        raw = RawConfig.load(paths, schema=self._schema)
        instance = bind(raw, "app.paths", PathsConfig)
        tls = bind(raw, "app.tls", TlsConfig)
        instance.layout(paths[0].resolve().parent, tls).apply()
        targets = await self.targets(raw, packages, section=section)
        applied = await self._migrations.apply(targets)
        for name in applied:
            sys.stdout.write(f"migrate: {name}\n")

        sys.stdout.write("migrate: completed\n")

    async def targets(
        self,
        raw: DictConfig,
        packages: Sequence[str],
        *,
        section: str = "app",
    ) -> Sequence[MigrationTarget]:
        """Общие планы для консоли и подготовки тестовых баз."""
        location = bind(raw, section, MigrationLocation)
        plan = MigrationScripts(location.db_schema).packages(packages)
        targets = [MigrationTarget(location.postgres, plan)]
        for source in self._sources:
            targets.extend(await source.targets(raw))

        return targets
