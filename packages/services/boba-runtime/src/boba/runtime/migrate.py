"""Команда migrate: конфиг приложения, планы плагинов и явный DDL PostgreSQL.

Ошибки:
ConfigError — секции конфига или план плагина не прошли проверку.
PostgresError — SQL-версия не читается или её выполнение завершилось ошибкой.
RuntimeError — программа плагина не отдала план создания хранилища.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from omegaconf import DictConfig
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from boba.config import ConfigError, ConfigSchema, bind
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.migrate import (
    MigrationScript,
    MigrationScripts,
    MigrationTarget,
    PostgresMigrations,
)
from boba.runtime.config import MigrationOptions, PathsConfig, RawConfig, TlsConfig


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
    """Собирает планы по конфигу и мигрирует хранилища без запуска приложения.

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
        self._schema = schema
        if self._schema is None:
            self._schema = ConfigSchema(MigrationConfig)

    async def run(
        self,
        paths: Sequence[Path],
        package: str,
        *,
        section: str = "app",
        options: MigrationOptions | None = None,
    ) -> None:
        if options is None:
            options = MigrationOptions()

        raw = RawConfig.load(paths, schema=self._schema)
        instance = bind(raw, "app.paths", PathsConfig)
        tls = bind(raw, "app.tls", TlsConfig)
        instance.layout(paths[0].resolve().parent, tls).apply()
        targets = await self.targets(
            raw, package, section=section, version=options.version
        )
        inputs = self._inputs(options.data)
        selected: list[MigrationTarget] = []
        for target in targets:
            connection = target.postgres
            plan = target.plan.model_copy(update={"inputs": inputs})
            if options.database is not None:
                connection = connection.model_copy(update={"dbname": options.database})

            if options.schema is not None:
                plan = plan.model_copy(update={"db_schema": options.schema})

            selected.append(MigrationTarget(connection, plan))

        applied = await self._migrations.apply(selected)
        for name in applied:
            sys.stdout.write(f"migrate: {name}\n")

        sys.stdout.write("migrate: completed\n")

    def _inputs(self, path: Path | None) -> tuple[MigrationScript, ...]:
        if path is None:
            return ()

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"reading migration data {path} failed: {exc}") from exc

        statement = sql.SQL(
            "select set_config('boba.migration.data', {}, true)"
        ).format(sql.Literal(json.dumps(data, ensure_ascii=False)))
        return (MigrationScript(name=str(path), text=statement.as_string()),)

    async def targets(
        self,
        raw: DictConfig,
        package: str,
        *,
        section: str = "app",
        version: str | None = None,
    ) -> Sequence[MigrationTarget]:
        """Общие планы для консоли и подготовки тестовых баз."""
        location = bind(raw, section, MigrationLocation)
        scripts = MigrationScripts(location.db_schema)
        plan = scripts.application(package, version=version)
        targets = [MigrationTarget(location.postgres, plan)]
        for source in self._sources:
            targets.extend(await source.targets(raw))

        return targets
