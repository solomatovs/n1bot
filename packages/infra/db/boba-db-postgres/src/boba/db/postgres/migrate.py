"""Создание и версионное обновление PostgreSQL по планам владельцев схем.

Ошибки:
PostgresError — целевая схема содержит таблицы, план противоречив либо БД
    отклонила подключение, проверку или создание объектов.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    """План схемы без credentials: начальный DDL, версии и переход со старых таблиц."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    db_schema: str = Field(alias="schema", min_length=1)
    connection: str = "connection"
    scripts: tuple[MigrationScript, ...] = Field(min_length=1)
    stream: str = ""
    upgrades: tuple[MigrationScript, ...] = ()
    adoption: tuple[MigrationScript, ...] = ()
    inputs: tuple[MigrationScript, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> MigrationPlan:
        versions = [script.name for script in self.upgrades]
        if versions != sorted(set(versions)):
            raise ValueError(
                "migration upgrades must have unique names in version order"
            )

        if not self.stream:
            if self.upgrades or self.adoption or self.inputs:
                raise ValueError(
                    "migration upgrades, adoption and inputs require a stream"
                )

        return self


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

    def application(self, package: str) -> MigrationPlan:
        baseline = self.package(package)
        upgrades = self._directory(package, "upgrades")
        adoption = self._directory(package, "adoption")

        return MigrationPlan(
            schema=self._schema.name,
            scripts=tuple(baseline),
            stream=package,
            upgrades=tuple(upgrades),
            adoption=tuple(adoption),
        )

    def _directory(self, package: str, directory: str) -> Sequence[MigrationScript]:
        source = files(package) / directory
        if not source.is_dir():
            return ()

        scripts: list[MigrationScript] = []
        for path in self._schema.files_in(source):
            query = PgQueryBuilder(schema=self._schema.ident).from_file(path).build()
            scripts.append(self.statement(path.name, query))

        return scripts

    def statement(self, name: str, query: PgQuery) -> MigrationScript:
        if query.params:
            msg = f"migration script {name}: expected rendered SQL without parameters"
            raise PostgresError(msg)

        return MigrationScript(name=name, text=query.text.as_string())


class PostgresMigrations:
    """Исполняет независимые потоки миграций под DDL-замком схемы.

    Версионный план хранит контрольные суммы в schema_migrations. Без stream
    план требует пустую схему. Один сбой откатывает изменения текущей схемы.
    """

    async def apply(self, targets: Sequence[MigrationTarget]) -> Sequence[str]:
        groups = self._grouped(targets)
        applied: list[str] = []
        for group in groups:
            target = group[0]
            try:
                async with (
                    await AsyncPostgresPool.dedicated(target.postgres) as conn,
                    conn.transaction(),
                ):
                    schema = PostgresSchema(target.plan.db_schema)
                    await schema.ddl_lock().acquire(conn)
                    await schema.ensure(conn)
                    for member in group:
                        applied.extend(await self._apply(conn, member))
            except psycopg.Error as exc:
                msg = (
                    f"migrate {target.postgres.where()} "
                    f"schema {target.plan.db_schema}: "
                    f"transaction failed: {exc}"
                )
                raise PostgresError(msg) from exc

        return applied

    async def _apply(
        self, conn: psycopg.AsyncConnection[Any], target: MigrationTarget
    ) -> Sequence[str]:
        plan = target.plan
        schema = PostgresSchema(plan.db_schema)
        if not plan.stream:
            await schema.require_empty(conn)
            return await self._scripts(conn, target, plan.scripts)

        cursor = await conn.execute(
            "select tablename from pg_tables where schemaname = %s", (plan.db_schema,)
        )
        tables = {row[0] for row in await cursor.fetchall()}
        query = (
            PgQueryBuilder(schema=schema.ident)
            .add("""
            create table if not exists {schema}.schema_migrations (
                stream text not null,
                version text not null,
                checksum text not null,
                applied_at timestamptz not null default now(),
                primary key (stream, version)
            )
        """)
            .build()
        )
        await conn.execute(query.text)
        query = (
            PgQueryBuilder(schema=schema.ident)
            .add(
                """
            select version, checksum from {schema}.schema_migrations
            where stream = %(stream)s order by version
        """,
                stream=plan.stream,
            )
            .build()
        )
        cursor = await conn.execute(query.text, query.params)
        history = dict(await cursor.fetchall())
        expected = {"0000_baseline", *(script.name for script in plan.upgrades)}
        unknown = history.keys() - expected
        if unknown:
            raise PostgresError(
                f"migration stream {plan.stream!r}: database has unknown versions "
                f"{sorted(unknown)}; use the application version that owns them"
            )

        applied: list[str] = []
        baseline = self._digest(plan.scripts)
        if "0000_baseline" not in history:
            if tables - {"schema_migrations"}:
                if not plan.adoption:
                    raise PostgresError(
                        f"migration stream {plan.stream!r}: schema {plan.db_schema!r} "
                        "has tables but no version history; "
                        "an adoption plan is required"
                    )

                await self._scripts(conn, target, plan.adoption)

            applied.extend(await self._scripts(conn, target, plan.scripts))
            await self._record(conn, target, "0000_baseline", baseline)
        else:
            self._unchanged(plan.stream, "0000_baseline", baseline, history)

        pending = expected - history.keys() - {"0000_baseline"}
        if pending:
            await self._scripts(conn, target, plan.inputs)

        for script in plan.upgrades:
            digest = self._digest((script,))
            if script.name in history:
                self._unchanged(plan.stream, script.name, digest, history)
                continue

            later = [version for version in history if version > script.name]
            if later:
                raise PostgresError(
                    f"migration stream {plan.stream!r}: "
                    f"unapplied version {script.name!r} "
                    f"precedes already applied {later}"
                )

            applied.extend(await self._scripts(conn, target, (script,)))
            await self._record(conn, target, script.name, digest)

        return applied

    async def _scripts(
        self,
        conn: psycopg.AsyncConnection[Any],
        target: MigrationTarget,
        scripts: Sequence[MigrationScript],
    ) -> Sequence[str]:
        applied: list[str] = []
        for script in scripts:
            query = PgQueryBuilder().raw_query(script.text).build()
            try:
                await conn.execute(query.text, prepare=False)
            except psycopg.Error as exc:
                raise PostgresError(
                    f"migrate {target.plan.stream} schema {target.plan.db_schema}: "
                    f"applying {script.name} failed: {exc}"
                ) from exc

            applied.append(f"{target.plan.db_schema}: {script.name}")

        return applied

    async def _record(
        self,
        conn: psycopg.AsyncConnection[Any],
        target: MigrationTarget,
        version: str,
        checksum: str,
    ) -> None:
        schema = PostgresSchema(target.plan.db_schema)
        query = (
            PgQueryBuilder(schema=schema.ident)
            .add(
                """
            insert into {schema}.schema_migrations (stream, version, checksum)
            values (%(stream)s, %(version)s, %(checksum)s)
        """,
                stream=target.plan.stream,
                version=version,
                checksum=checksum,
            )
            .build()
        )
        await conn.execute(query.text, query.params)

    def _unchanged(
        self, stream: str, version: str, checksum: str, history: Mapping[str, str]
    ) -> None:
        if history[version] != checksum:
            raise PostgresError(
                f"migration stream {stream!r}: applied version {version!r} was edited; "
                "restore that resource and add a new upgrade"
            )

    def _digest(self, scripts: Sequence[MigrationScript]) -> str:
        digest = sha256()
        for script in scripts:
            digest.update(script.name.encode())
            digest.update(b"\0")
            digest.update(script.text.encode())
            digest.update(b"\0")

        return digest.hexdigest()

    def _grouped(
        self, targets: Sequence[MigrationTarget]
    ) -> Sequence[Sequence[MigrationTarget]]:
        grouped: dict[tuple[str, str], dict[str, MigrationTarget]] = {}
        for target in targets:
            key = (target.postgres.where(), target.plan.db_schema)
            streams = grouped.setdefault(key, {})
            previous = streams.get(target.plan.stream)
            if previous is None:
                streams[target.plan.stream] = target
                continue

            if target.plan.stream:
                if previous.plan != target.plan:
                    raise PostgresError(
                        f"conflicting migration plans for {target.plan.stream}"
                    )
                continue

            scripts = {script.name: script for script in previous.plan.scripts}
            for script in target.plan.scripts:
                if script.name in scripts:
                    if scripts[script.name] != script:
                        raise PostgresError(
                            f"conflicting migration SQL for {script.name}"
                        )

                scripts[script.name] = script

            plan = target.plan.model_copy(update={"scripts": tuple(scripts.values())})
            streams[target.plan.stream] = MigrationTarget(target.postgres, plan)

        return [tuple(grouped[key].values()) for key in sorted(grouped)]
