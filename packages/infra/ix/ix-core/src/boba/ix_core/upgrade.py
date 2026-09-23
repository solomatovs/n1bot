"""Накат схемы в базу ix: ядро пакетом ix-core, свои таблицы каждым пакетом.

Файлы schema/*.sql применяются по порядку имён, каждый файл одной командой
psycopg в autocommit; схема подставляется в `{schema}` перед отправкой. Файл
самодостаточен: значения enum, добавленные `alter type ... add value`, нельзя
использовать в своей же транзакции, поэтому пакет кладёт их отдельным файлом,
а их использование следующим. Файлы идемпотентны (`if not exists`,
`on conflict do nothing`), повторный накат безопасен.

После файлов проверяется всё, что владельцы объявили в ядре: тело объявления аспекта
выполняется с limit 0 и сверяется с контрактом, таблица индекса — на существование и
колонки своего вида, формула ссылки — на разбор подстановок, промпт описания — на роль
модели и место под материал. Опечатка владельца валит его же накат, а не прогон
потребителя.

Ошибки:
SchemaUpgradeError — база недоступна, каталога схемы нет, сервер отклонил DDL,
    ядро ix отсутствует там, где пакет на него опирается, объявление аспекта,
    строка реестра индексов, формула ссылки или промпт описания нарушает контракт.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.postgres import types as pg_types

from boba.db.postgres import AsyncPostgresPool, PostgresError
from boba.db.postgres.query import PgQueryBuilder
from boba.db.postgres.schema import PostgresSchema
from boba.ix_core.aspects import (
    AspectDeclarationError,
    ContractColumn,
    ContractType,
    SurfaceAspect,
)
from boba.ix_core.database import IxDatabase
from boba.ix_core.indexes import IndexTable, IndexTableError, index_columns
from boba.ix_core.prompts import SurfacePromptError
from boba.ix_core.registry import IxRegistry
from boba.ix_core.urls import SurfaceUrlError
from boba.toolkit.sql import QueryBuildError

__all__ = [
    "SchemaUpgrade",
    "SchemaUpgradeError",
    "UpgradeReport",
]

logger = logging.getLogger("ix-upgrade")


class SchemaUpgradeError(Exception):
    """Схему не удалось применить."""


@dataclass(frozen=True, kw_only=True)
class UpgradeReport:
    """Итог наката: какие файлы применены."""

    files: Sequence[str]


class SchemaUpgrade:
    """Применяет schema/*.sql одного пакета в базу ix.

    Пакет отдаёт свой каталог схемы и, если его таблицы ссылаются на ядро,
    просит проверить ядро: без него DDL упал бы на первом references, и по
    сообщению сервера было бы непонятно, что делать. База, схема и таймауты
    сессии приходят секцией IxDatabase приложения.
    """

    def __init__(self, schema_dir: Path, *, requires_core: bool = True) -> None:
        self._schema_dir = schema_dir
        self._requires_core = requires_core

    async def run(self, database: IxDatabase) -> UpgradeReport:
        files = self._files()

        try:
            async with await AsyncPostgresPool.dedicated(database.postgres) as conn:
                if self._requires_core:
                    await self._validate_core_layer_exists(conn, database.db_schema)

                for path in files:
                    logger.info("applying %s", path.name)
                    query = (
                        PgQueryBuilder(schema=sql.Identifier(database.db_schema))
                        .read(path)
                        .build()
                    )
                    await conn.execute(query.text, query.params)

                registry = IxRegistry(database.db_schema)
                await self._check_declarations(conn, registry)
                await self._check_index_tables(conn, registry)
                await self._check_urls(conn, registry)
                await self._check_prompts(conn, registry)

        except PostgresError as exc:
            msg = (
                f"upgrade {self._schema_dir}: ix database "
                f"{database.postgres.where()}: {exc}"
            )
            raise SchemaUpgradeError(msg) from exc
        except psycopg.Error as exc:
            msg = f"upgrade {self._schema_dir}: applying schema failed: {exc}"
            raise SchemaUpgradeError(msg) from exc
        except AspectDeclarationError as exc:
            msg = f"upgrade {self._schema_dir}: aspect declaration rejected: {exc}"
            raise SchemaUpgradeError(msg) from exc
        except IndexTableError as exc:
            msg = f"upgrade {self._schema_dir}: index table rejected: {exc}"
            raise SchemaUpgradeError(msg) from exc
        except SurfaceUrlError as exc:
            msg = f"upgrade {self._schema_dir}: url template rejected: {exc}"
            raise SchemaUpgradeError(msg) from exc
        except SurfacePromptError as exc:
            msg = f"upgrade {self._schema_dir}: describe prompt rejected: {exc}"
            raise SchemaUpgradeError(msg) from exc

        return UpgradeReport(files=[path.name for path in files])

    def _files(self) -> list[Path]:
        if not self._schema_dir.is_dir():
            msg = f"upgrade: schema directory {self._schema_dir} does not exist"
            raise SchemaUpgradeError(msg)

        files = sorted(self._schema_dir.glob("*.sql"))
        if not files:
            msg = f"upgrade: no *.sql files under {self._schema_dir}"
            raise SchemaUpgradeError(msg)

        return files

    async def _check_declarations(
        self, conn: psycopg.AsyncConnection[Any], registry: IxRegistry
    ) -> None:
        schema = PostgresSchema(registry.db_schema)
        if not await schema.has_table(conn, "surface_aspect"):
            return

        declarations = await registry.read_declarations(conn, ())
        for declaration in declarations:
            await self._check_body(conn, registry.db_schema, declaration)

        logger.info("aspect declarations verified: %d", len(declarations))

    async def _check_body(
        self,
        conn: psycopg.AsyncConnection[Any],
        db_schema: str,
        declaration: SurfaceAspect,
    ) -> None:
        """Тело объявления выполняется и отдаёт node_id bigint и content text."""
        where = f"aspect {declaration.aspect} of surface {declaration.surface}"

        try:
            body = (
                PgQueryBuilder(schema=sql.Identifier(db_schema))
                .add(declaration.body)
                .build()
            )
        except QueryBuildError as exc:
            raise AspectDeclarationError(f"{where}: body: {exc}") from exc

        probe = (
            PgQueryBuilder()
            .add("select * from ({body}) s limit 0", body=body.text)
            .build()
        )
        try:
            cur = await conn.execute(probe.text, probe.params)
        except psycopg.Error as exc:
            raise AspectDeclarationError(f"{where}: body does not run: {exc}") from exc

        if cur.description is None:
            raise AspectDeclarationError(f"{where}: body returns no result set")

        names: list[str] = []
        type_names: list[str] = []
        for column in cur.description:
            names.append(column.name)
            info = pg_types.get(column.type_code)
            if info is None:
                type_names.append(f"oid {column.type_code}")
                continue

            type_names.append(info.name)

        expected = [ContractColumn.NODE_ID, ContractColumn.CONTENT]
        if names != expected:
            raise AspectDeclarationError(
                f"{where}: body must return columns {expected}, got {names}"
            )

        if type_names[0] != ContractType.INT8:
            raise AspectDeclarationError(
                f"{where}: {ContractColumn.NODE_ID} must be {ContractType.INT8}, "
                f"got {type_names[0]}"
            )

        content_types = frozenset({ContractType.TEXT, ContractType.VARCHAR})
        if type_names[1] not in content_types:
            raise AspectDeclarationError(
                f"{where}: {ContractColumn.CONTENT} must be one of "
                f"{sorted(content_types)}, got {type_names[1]}"
            )

    async def _check_index_tables(
        self, conn: psycopg.AsyncConnection[Any], registry: IxRegistry
    ) -> None:
        schema = PostgresSchema(registry.db_schema)
        if not await schema.has_table(conn, "index_table"):
            return

        await registry.read_tables(conn)
        tables = registry.get_tables()
        for table in tables:
            await self._check_index_table(conn, registry.db_schema, table)

        logger.info("index tables verified: %d", len(tables))

    async def _check_index_table(
        self, conn: psycopg.AsyncConnection[Any], db_schema: str, table: IndexTable
    ) -> None:
        """Таблица индекса существует и несёт колонки своего вида."""
        where = f"index table {table.name} of kind {table.kind} (owner {table.owner})"

        probe = (
            PgQueryBuilder(schema=sql.Identifier(db_schema))
            .add("select 1 from {schema}.{name} limit 0", name=table.ident())
            .build()
        )
        try:
            await conn.execute(probe.text, probe.params)
        except psycopg.Error as exc:
            raise IndexTableError(f"{where}: table is not readable: {exc}") from exc

        query = (
            PgQueryBuilder()
            .add(
                """
                select
                    c.column_name
                from
                    information_schema.columns c
                where 1=1
                    and c.table_schema = %(schema_name)s
                    and c.table_name = %(name)s
                """,
                schema_name=db_schema,
                name=table.name,
            )
            .build()
        )
        cur = await conn.execute(query.text, query.params)
        present: set[str] = set()
        async for row in cur:
            present.add(str(row[0]))

        missing: list[str] = []
        for column in index_columns(table.kind):
            if column not in present:
                missing.append(column)

        if not missing:
            return

        raise IndexTableError(
            f"{where}: expected columns {list(index_columns(table.kind))}, "
            f"missing {missing}"
        )

    async def _check_urls(
        self, conn: psycopg.AsyncConnection[Any], registry: IxRegistry
    ) -> None:
        schema = PostgresSchema(registry.db_schema)
        if not await schema.has_table(conn, "surface_url"):
            return

        await registry.read_urls(conn)
        surfaces = registry.get_urls()
        logger.info("url templates verified: %d", len(surfaces))

    async def _check_prompts(
        self, conn: psycopg.AsyncConnection[Any], registry: IxRegistry
    ) -> None:
        schema = PostgresSchema(registry.db_schema)
        if not await schema.has_table(conn, "surface_prompt"):
            return

        await registry.read_prompts(conn)
        prompts = registry.get_prompts()
        for prompt in prompts:
            prompt.check()

        logger.info("describe prompts verified: %d", len(prompts))

    async def _validate_core_layer_exists(
        self, conn: psycopg.AsyncConnection[Any], db_schema: str
    ) -> None:
        for table in ("node", "edge"):
            if await PostgresSchema(db_schema).has_table(conn, table):
                continue

            raise SchemaUpgradeError(
                f"upgrade: core table {db_schema}.{table} is missing in the database; "
                "apply the core first: boba-ix-core upgrade --config <config>"
            )
