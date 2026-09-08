"""Общий каркас хранилищ каталога на Postgres: имена таблиц и колонок в SQL
по enum'ам, граница ошибок слоя, транзакция с курсором словарей, разбор
строк в модели и проверка returning. ProcessStore и ConnectionStore
наследуют его и добавляют свои таблицы.

Ошибки:
CatalogStoreError — Postgres недоступен, ответ битый, строка не
    складывается в модель.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterable, Mapping
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar, LiteralString, TypeVar
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from pydantic import BaseModel, ValidationError

from boba.catalog_service.config import CatalogConfig
from boba.catalog_service.records import CatalogStoreError
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresTable, SqlNames
from boba.db.postgres.catalog import CatalogDomainError

logger = logging.getLogger(__name__)

__all__ = ["CatalogStoreBase", "Cursor", "ModelT"]

Cursor = psycopg.AsyncCursor[DictRow]
ModelT = TypeVar("ModelT", bound=BaseModel)


class CatalogStoreBase(PostgresTable):
    """База хранилищ каталога: наследник объявляет TABLES (enum таблиц своей
    схемы), PREFIXED (префикс → enum колонок для {prefix_column} в SQL) и
    COLUMN_LISTS (готовые списки колонок вроде {draft_columns}); каркас даёт
    _sql, _guarded, _transaction, _parse, _parse_all, _returning,
    _advisory_lock и _max_of."""

    TABLES: ClassVar[type[StrEnum]]
    PREFIXED: ClassVar[Mapping[str, type[StrEnum]]] = {}
    COLUMN_LISTS: ClassVar[Mapping[str, LiteralString]] = {}
    LABEL: ClassVar[str] = "catalog"
    """Подпись слоя в текстах ошибок."""

    def __init__(
        self, cfg: CatalogConfig, schema: str, pool: AsyncPostgresPool | None = None
    ) -> None:
        postgres = cfg.connection
        if pool is None:
            postgres = cfg.require_conn()

        super().__init__(postgres, schema, pool)
        self._cfg = cfg

    def _named_table(self, table: StrEnum) -> sql.Identifier:
        """Идентификатор таблицы из TABLES; наследник с чужой схемой переопределяет."""
        return self._table(table)

    def _sql(self, text: LiteralString) -> sql.Composed:
        """SQL с именами таблиц по значению enum, колонок с префиксом
        ({dr_id}) и готовых списков колонок ({draft_columns})."""
        names: dict[str, sql.Composable] = {}
        for table in self.TABLES:
            names[table.value] = self._named_table(table)

        for prefix, columns in self.PREFIXED.items():
            for column in columns:
                names[f"{prefix}_{column.value}"] = SqlNames.ident(column)

        for name, columns in self.COLUMN_LISTS.items():
            names[name] = sql.SQL(columns).format(**names)

        return sql.SQL(text).format(**names)

    @asynccontextmanager
    async def _guarded(self, action: str) -> AsyncGenerator[None, None]:
        """Граница слоя: отказ базы, пула или домена уходит наружу как
        CatalogStoreError."""
        try:
            yield
        except (psycopg.Error, PostgresError, CatalogDomainError) as exc:
            msg = f"{self.LABEL}: {action} in schema {self._schema} failed: {exc}"
            raise CatalogStoreError(msg) from exc

    @asynccontextmanager
    async def _transaction(self, action: str) -> AsyncGenerator[Cursor]:
        """Курсор словарей внутри одной транзакции на выделенном соединении."""
        pool = await self._pool()
        async with (
            self._guarded(action),
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            yield cur

    def _parse(self, model: type[ModelT], row: Mapping[str, Any]) -> ModelT:
        """Ошибки:
        CatalogStoreError — строка не складывается в модель.
        """
        try:
            return model.model_validate(dict(row))
        except ValidationError as exc:
            msg = (
                f"{self.LABEL}: row {dict(row)!r} from {self._schema} does not "
                f"form a valid {model.__name__}: {exc}"
            )
            raise CatalogStoreError(msg) from exc

    def _parse_all(
        self, model: type[ModelT], rows: Iterable[Mapping[str, Any]]
    ) -> list[ModelT]:
        parsed: list[ModelT] = []
        for row in rows:
            parsed.append(self._parse(model, row))

        return parsed

    def _returning(self, row: DictRow | None, what: str) -> DictRow:
        """Строка после insert/update … returning; её отсутствие — отказ хранилища."""
        if row is None:
            msg = f"{self.LABEL}: {what} in {self._schema} returned no row"
            raise CatalogStoreError(msg)

        return row

    async def _advisory_lock(self, cur: Cursor, prefix: str, key: UUID) -> None:
        """Транзакционный advisory-замок по ключу; снимается с концом транзакции."""
        await cur.execute(
            "select pg_advisory_xact_lock(hashtext(%(key)s))",
            {"key": f"{self._schema}.{prefix}.{key}"},
        )

    async def _max_of(
        self,
        cur: Cursor,
        table: StrEnum,
        column: StrEnum,
        key_column: StrEnum,
        key: UUID,
    ) -> int:
        """Наибольшее значение колонки среди строк с ключом; 0 — строк нет."""
        await cur.execute(
            sql.SQL(
                "select coalesce(max({}), 0) as top from {} where {} = %(key)s"
            ).format(
                SqlNames.ident(column),
                self._named_table(table),
                SqlNames.ident(key_column),
            ),
            {"key": key},
        )
        row = self._returning(
            await cur.fetchone(),
            f"reading max {column.value} of {table.value} for {key_column.value} {key}",
        )

        return int(row["top"])
