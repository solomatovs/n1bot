"""Общая часть хранилища описаний: конфиг, сессия с готовой схемой, ключ области.

Сущности живут в своих модулях (nodes, edges) со своими SQL, моделями,
ошибками и инструментами; отсюда они берут только сессию — соединение с
подготовленными таблицами и сборщик запросов со схемой — и ключ области.
Схему и обе таблицы сессия готовит идемпотентно на каждом вызове: внешний
потребитель забирает строки и делает truncate, схему и таблицы не трогает.

Ошибки:
DescriberError — область вызова не годится ключом: id не uuid.
PostgresError — до базы приложения не достучаться.
psycopg.Error — СУБД отклонила запрос.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres import PayloadPostgres, PgQueryBuilder, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import Scope, ScopeKind
from boba.toolkit.types import SecretRevealing

logger = logging.getLogger(__name__)

__all__ = [
    "DescriberError",
    "DescriberErrorKind",
    "DescriberSession",
    "DescriberStore",
    "DescriberToolConfig",
    "MissingIds",
    "ScopeKey",
    "WriteAction",
]


class DescriberToolConfig(SecretRevealing):
    """Секция [tool.describer]: база приложения и схема таблиц node/edge."""

    SECTION: ClassVar[str] = "tool.describer"

    connection: PostgresConfig = Field(
        description="Подключение к базе приложения, где лежат таблицы описаний.",
    )
    db_schema: str = Field(min_length=1, description="Схема таблиц node и edge.")


class DescriberError(Exception):
    """Область вызова не годится ключом хранилища."""


class DescriberErrorKind(StrEnum):
    """Ожидаемые отказы инструментов describer: карты EXPECTED модулей."""

    INVALID_ADDRESS = "invalid_address"
    NODE_MISSING = "node_missing"
    NODE_ID_MISSING = "node_id_missing"
    EDGE_ID_MISSING = "edge_id_missing"
    INVALID_SCOPE = "invalid_scope"
    DATABASE_UNAVAILABLE = "database_unavailable"
    SQL_FAILED = "sql_failed"


class WriteAction(StrEnum):
    """Что сделал upsert."""

    INSERTED = "inserted"
    UPDATED = "updated"

    @classmethod
    def of(cls, inserted: bool) -> WriteAction:
        if inserted:
            return cls.INSERTED

        return cls.UPDATED


class ScopeKey(BaseModel):
    """Область вызова ключом таблиц: вид и uuid."""

    model_config = ConfigDict(frozen=True)

    kind: ScopeKind
    id: UUID

    @classmethod
    def of(cls, scope: Scope) -> ScopeKey:
        try:
            scope_id = scope.uuid()
        except ValueError as exc:
            msg = f"describer: {exc}; descriptions are keyed by uuid scopes"
            raise DescriberError(msg) from exc

        return cls(kind=scope.kind, id=scope_id)


class MissingIds:
    """Какие из запрошенных id не нашлись: порядок запроса сохраняется."""

    def __init__(self, wanted: Sequence[int], found: set[int]) -> None:
        self._wanted = wanted
        self._found = found

    def ids(self) -> list[int]:
        missing: list[int] = []
        for record_id in self._wanted:
            if record_id not in self._found:
                missing.append(record_id)

        return missing


class DescriberSession:
    """Соединение с готовыми таблицами и сборщик запросов со схемой стоящим
    именем {schema}: таблицы node и edge пишутся в SQL как {schema}.node."""

    def __init__(
        self, conn: psycopg.AsyncConnection[Any], schema: PostgresSchema
    ) -> None:
        self.conn = conn
        self._schema = schema

    def query(self) -> PgQueryBuilder:
        return PgQueryBuilder(schema=self._schema.ident)


class DescriberStore:
    """Сессии хранилища: одно соединение на вызов инструмента, схема и
    таблицы готовы к первому запросу."""

    def __init__(self, cfg: DescriberToolConfig) -> None:
        self._connection = cfg.connection
        self._schema = PostgresSchema(cfg.db_schema)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[DescriberSession, None]:
        """Сессия с готовыми таблицами; соединение закрывается по выходу."""
        conn = await PayloadPostgres.connect_config(self._connection)
        async with conn:
            await self._ensure(conn)
            yield DescriberSession(conn, self._schema)

    async def _ensure(self, conn: psycopg.AsyncConnection[Any]) -> None:
        """Схема и таблицы под одним advisory-замком: параллельные вызовы одного
        ответа модели иначе роняют create schema if not exists на уникальности
        pg_namespace. Без права на create schema её заводит администратор."""
        tables = (
            PgQueryBuilder(schema=self._schema.ident)
            .add(
                """
                create table if not exists {schema}.node (
                    id          bigserial primary key,
                    scope_kind  varchar not null,
                    scope_id    uuid not null,
                    kind        varchar not null,
                    address     jsonb not null,
                    url_address varchar not null,
                    description varchar not null,
                    s__wrt_ts   timestamptz not null default now()
                );
                create unique index if not exists node_uk
                    on {schema}.node (scope_id, address);
                create index if not exists node_address_gin
                    on {schema}.node using gin (address jsonb_path_ops);
                create index if not exists node_kind_btree on {schema}.node (kind);
                create table if not exists {schema}.edge (
                    id          bigserial primary key,
                    source_id   bigint not null
                                references {schema}.node on delete cascade,
                    target_id   bigint not null
                                references {schema}.node on delete cascade,
                    kind        varchar not null,
                    description varchar not null,
                    s__wrt_ts   timestamptz not null default now(),
                    unique (source_id, target_id, kind)
                )
                """
            )
            .build()
        )

        async with conn.transaction():
            await self._schema.ddl_lock().acquire(conn)
            await self._schema.ensure(conn)
            await conn.execute(tables.text, tables.params, prepare=False)
