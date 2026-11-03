"""Явное создание таблиц в пустых схемах PostgreSQL по планам приложений.

Ошибки:
PostgresError — целевая схема содержит таблицы, план противоречив либо БД
    отклонила подключение, проверку или создание объектов.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres.async_pool import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.errors import PostgresError
from boba.db.postgres.query import PgQuery, PgQueryBuilder
from boba.db.postgres.schema import PostgresSchema


class MigrationScript(BaseModel):
    """SQL одного ресурса пакета; процесс плагина передаёт его команде migrate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    text: str = Field(min_length=1)


class MigrationPlan(BaseModel):
    """План создания схемы без учётных данных; connection указывает секцию конфига."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    db_schema: str = Field(alias="schema", min_length=1)
    connection: str = "connection"
    scripts: tuple[MigrationScript, ...] = Field(min_length=1)


@dataclass(frozen=True)
class MigrationTarget:
    """План с подключением, разрешённым из конфига владельцем команды migrate."""

    postgres: PostgresConfig
    plan: MigrationPlan


class MigrationScripts:
    """Собирает SQL ресурсов пакетов без обращения к БД для владельцев схем."""

    def __init__(self, schema: str) -> None:
        self._schema = PostgresSchema(schema)

    def packages(self, packages: Sequence[str]) -> MigrationPlan:
        scripts: list[MigrationScript] = []
        for package in packages:
            scripts.extend(self.package(package))

        return MigrationPlan(schema=self._schema.name, scripts=tuple(scripts))

    def package(
        self, package: str, names: Mapping[str, sql.Composable] | None = None
    ) -> Sequence[MigrationScript]:
        substitutions: dict[str, sql.Composable] = {}
        if names is not None:
            substitutions.update(names)

        source = files(package) / "schema"
        scripts: list[MigrationScript] = []
        for path in self._schema.files_in(source):
            query = (
                PgQueryBuilder(schema=self._schema.ident, **substitutions)
                .from_file(path)
                .build()
            )
            scripts.append(self.statement(f"{package}/{path.name}", query))

        return scripts

    def statement(self, name: str, query: PgQuery) -> MigrationScript:
        if query.params:
            msg = f"migration script {name}: expected rendered SQL without parameters"
            raise PostgresError(msg)

        return MigrationScript(name=name, text=query.text.as_string())


class PostgresMigrations:
    """Проверяет все целевые схемы до DDL и создаёт таблицы одной транзакцией на схему.

    Команды приложений передают свои планы и планы плагинов. Совпадающие
    ресурсы общего хранилища исполняются один раз; серверы этот класс не вызывают.
    """

    async def apply(self, targets: Sequence[MigrationTarget]) -> Sequence[str]:
        groups = self._grouped(targets)
        applied: list[str] = []
        try:
            async with AsyncExitStack() as stack:
                opened: list[tuple[MigrationTarget, psycopg.AsyncConnection[Any]]] = []
                for target in groups:
                    conn = await stack.enter_async_context(
                        await AsyncPostgresPool.dedicated(target.postgres)
                    )
                    await stack.enter_async_context(conn.transaction())
                    schema = PostgresSchema(target.plan.db_schema)
                    await schema.ddl_lock().acquire(conn)
                    await schema.require_empty(conn)
                    opened.append((target, conn))

                for target, conn in opened:
                    await PostgresSchema(target.plan.db_schema).ensure(conn)
                    for script in target.plan.scripts:
                        query = PgQueryBuilder().raw_query(script.text).build()
                        try:
                            await conn.execute(query.text, prepare=False)
                        except psycopg.Error as exc:
                            msg = (
                                f"migrate {target.postgres.where()} schema "
                                f"{target.plan.db_schema}: applying {script.name} "
                                f"failed: {exc}"
                            )
                            raise PostgresError(msg) from exc

                        applied.append(f"{target.plan.db_schema}: {script.name}")
        except PostgresError:
            raise
        except psycopg.Error as exc:
            msg = f"creating PostgreSQL schemas from the migration plan failed: {exc}"
            raise PostgresError(msg) from exc

        return applied

    def _grouped(self, targets: Sequence[MigrationTarget]) -> Sequence[MigrationTarget]:
        grouped: dict[tuple[str, str], MigrationTarget] = {}
        scripts: dict[tuple[str, str], dict[str, MigrationScript]] = {}
        for target in targets:
            key = (target.postgres.where(), target.plan.db_schema)
            grouped.setdefault(key, target)
            collected = scripts.setdefault(key, {})
            for script in target.plan.scripts:
                previous = collected.get(script.name)
                if previous is not None:
                    if previous != script:
                        msg = (
                            f"migration schema {target.plan.db_schema}: resource "
                            f"{script.name} has conflicting SQL in the supplied plans"
                        )
                        raise PostgresError(msg)

                collected[script.name] = script

        result: list[MigrationTarget] = []
        for key in sorted(grouped):
            target = grouped[key]
            plan = target.plan.model_copy(
                update={"scripts": tuple(scripts[key].values())}
            )
            result.append(MigrationTarget(target.postgres, plan))

        return result
