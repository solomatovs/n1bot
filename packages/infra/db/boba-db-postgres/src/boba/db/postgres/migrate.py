"""Выполнение SQL выбранной версии без учёта истории состояния БД.

Ошибки:
PostgresError — версия или SQL-ресурс недоступны, подключение либо SQL отказали.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import PurePath

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres.async_pool import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.errors import PostgresError
from boba.db.postgres.query import PgQueryBuilder
from boba.db.postgres.schema import PostgresSchema
from boba.toolkit.sql import QueryBuildError


class MigrationScript(BaseModel):
    """SQL-ресурс; шаблон раскрывается только для выбранной целевой схемы."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1)
    text: str = Field(min_length=1)


class MigrationPlan(BaseModel):
    """Папка SQL одного компонента, передаваемая также процессом плагина."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    db_schema: str = Field(alias="schema", min_length=1)
    connection: str = "connection"
    component: str = Field(min_length=1)
    version: str = Field(min_length=1)
    scripts: tuple[MigrationScript, ...] = Field(min_length=1)
    values: dict[str, str | int] = Field(default_factory=dict)
    identifiers: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    inputs: tuple[MigrationScript, ...] = ()


@dataclass(frozen=True)
class MigrationTarget:
    """План и подключение из конфига его владельца."""

    postgres: PostgresConfig
    plan: MigrationPlan


class MigrationScripts:
    """Читает выбранную папку migrations/<версия>/ в порядке имён файлов.

    Общие SQL-процедуры добавляются перед SQL владельца. Приложения и плагины
    используют один загрузчик; он не знает структуру их таблиц.
    """

    def __init__(self, schema: str) -> None:
        self._schema = schema

    def application(
        self,
        package: str,
        *,
        version: str | None = None,
        values: Mapping[str, str | int] | None = None,
        identifiers: Mapping[str, tuple[str, ...]] | None = None,
    ) -> MigrationPlan:
        try:
            root = files(package) / "migrations"
            versions = sorted(path.name for path in root.iterdir() if path.is_dir())
            if not versions:
                raise ValueError(f"no version directories under {root}")

            if version is None:
                version = versions[-1]

            if version not in versions:
                raise ValueError(
                    f"version {version!r} is absent; available: {versions}"
                )

            scripts = self._read("boba.db.postgres", "sql")
            scripts.extend(self._read(package, f"migrations/{version}"))
        except (ImportError, OSError, ValueError) as exc:
            raise PostgresError(
                f"reading SQL migrations of {package} failed: {exc}"
            ) from exc

        parameters: dict[str, str | int] = {}
        if values is not None:
            parameters.update(values)

        identifiers_values: dict[str, tuple[str, ...]] = {}
        if identifiers is not None:
            identifiers_values.update(identifiers)

        return MigrationPlan(
            schema=self._schema,
            component=package,
            version=version,
            scripts=tuple(scripts),
            values=parameters,
            identifiers=identifiers_values,
        )

    def _read(self, package: str, directory: str) -> list[MigrationScript]:
        source = files(package).joinpath(*PurePath(directory).parts)
        paths = PostgresSchema(self._schema).files_in(source)
        return [
            MigrationScript(
                name=f"{package}/{directory}/{path.name}",
                text=path.read_text(encoding="utf-8"),
            )
            for path in paths
        ]


class PostgresMigrations:
    """Исполняет планы по порядку, каждый в транзакции под замком целевой схемы.

    Внутренние объекты БД проверяет только SQL. Совпадающий план общего
    хранилища нескольких плагинов выполняется один раз за вызов apply.
    """

    async def apply(self, targets: Sequence[MigrationTarget]) -> Sequence[str]:
        applied: list[str] = []
        completed: list[MigrationTarget] = []
        for target in targets:
            if target in completed:
                continue

            applied.extend(await self._apply(target))
            completed.append(target)

        return applied

    async def _apply(self, target: MigrationTarget) -> Sequence[str]:
        plan = target.plan
        schema = PostgresSchema(plan.db_schema)
        substitutions: dict[str, sql.Composable] = {
            "schema": schema.ident,
            "schema_name_lit": sql.Literal(schema.name),
        }
        for name, value in plan.values.items():
            if name in substitutions:
                raise PostgresError(
                    f"migration {plan.component}: reserved SQL value {name!r}"
                )

            substitutions[name] = sql.Literal(value)

        for name, parts in plan.identifiers.items():
            if name in substitutions:
                raise PostgresError(
                    f"migration {plan.component}: duplicate SQL value {name!r}"
                )

            substitutions[name] = sql.Identifier(*parts)

        applied: list[str] = []
        context = f"migrate {target.postgres.where()} schema {schema.name}"
        try:
            async with (
                await AsyncPostgresPool.dedicated(target.postgres) as conn,
                conn.transaction(),
            ):
                await schema.ddl_lock().acquire(conn)
                await schema.ensure(conn)
                for script in plan.inputs:
                    context = (
                        f"migrate {target.postgres.where()} schema {schema.name}: "
                        f"{script.name}"
                    )
                    query = PgQueryBuilder().raw_query(script.text).build()
                    await conn.execute(query.text, prepare=False)

                for script in plan.scripts:
                    context = (
                        f"migrate {target.postgres.where()} schema {schema.name}: "
                        f"{script.name}"
                    )
                    query = PgQueryBuilder(**substitutions).add(script.text).build()
                    await conn.execute(query.text, prepare=False)
                    applied.append(f"{schema.name}: {script.name}")
        except (psycopg.Error, QueryBuildError, ValueError, KeyError) as exc:
            raise PostgresError(
                f"{context} failed; transaction rolled back: {exc}"
            ) from exc

        return applied
