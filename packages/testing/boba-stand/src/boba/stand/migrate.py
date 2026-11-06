"""Явная подготовка PostgreSQL тестами перед запуском приложений."""

from collections.abc import Sequence
from pathlib import Path

from psycopg import sql

from boba.config import ConfigLoader, ConfigSchema, bind
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.migrate import (
    MigrationPlan,
    MigrationScripts,
    MigrationTarget,
    PostgresMigrations,
)
from boba.db.postgres.migration_cli import (
    ApplicationMigrations,
    MigrationLocation,
)


class StandMigrations:
    """Тестовые фикстуры вызывают тот же механизм миграции, что и консольные команды."""

    def __init__(self) -> None:
        self._migrations = PostgresMigrations()

    async def storage(self, postgres: PostgresConfig, document: str) -> None:
        plan = MigrationPlan.model_validate_json(document)
        await self._migrations.apply((MigrationTarget(postgres, plan),))

    async def application(
        self, postgres: PostgresConfig, schema: str, package: str
    ) -> None:
        plan = MigrationScripts(schema).application(package)
        await self._migrations.apply((MigrationTarget(postgres, plan),))


class StandSchema:
    """Явная схема фикстуры сервиса: создать перед процессом, удалить после него.

    Удаляется только схема, успешно созданная этим объектом. При отказе
    migrate на непустой схеме её данные не затрагиваются.
    """

    def __init__(
        self,
        paths: Sequence[Path],
        package: str,
        migrations: ApplicationMigrations,
    ) -> None:
        self._paths = tuple(paths)
        self._package = package
        self._migrations = migrations
        self._config = ConfigLoader(paths, schema=ConfigSchema(MigrationLocation))
        self._created: MigrationLocation | None = None

    async def create(self) -> None:
        location = bind(self._config.load(), "app", MigrationLocation)
        async with await AsyncPostgresPool.dedicated(location.postgres) as conn:
            row = await (
                await conn.execute(
                    "select exists (select 1 from pg_namespace where nspname = %s)",
                    (location.db_schema,),
                )
            ).fetchone()
        if row is not None and row[0]:
            raise RuntimeError(
                f"stand schema {location.db_schema!r} already exists; "
                "refusing to migrate or drop it"
            )

        self._created = location
        try:
            await self._migrations.run(self._paths, self._package)
        except Exception:
            await self.drop()
            raise

    async def drop(self) -> None:
        location = self._created
        if location is None:
            return
        statement = sql.SQL("drop schema {} cascade").format(
            sql.Identifier(location.db_schema)
        )
        async with await AsyncPostgresPool.dedicated(location.postgres) as conn:
            await conn.execute(statement)
        self._created = None
