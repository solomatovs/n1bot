"""Общая часть хранилища описаний: конфиг, сессия с готовой схемой, ключ области.

Сущности живут в своих модулях (nodes, edges) со своими SQL, моделями,
ошибками и инструментами; отсюда они берут только сессию — соединение с
подготовленными таблицами — и ключ области. Схему и обе таблицы сессия
готовит идемпотентно на каждом вызове: внешний потребитель забирает строки
и делает truncate, схему и таблицы не трогает.

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
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres import PayloadPostgres
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
    "SqlNames",
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


class DescriberTable(StrEnum):
    """Таблицы хранилища; плейсхолдеры {node} и {edge} в текстах SQL."""

    NODE = "node"
    EDGE = "edge"


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
            scope_id = UUID(scope.id)
        except ValueError as exc:
            msg = (
                f"describer: scope {scope.kind.value} id {scope.id!r} is not a uuid, "
                "descriptions are keyed by uuid scopes"
            )
            raise DescriberError(msg) from exc

        return cls(kind=scope.kind, id=scope_id)


class MissingIds:
    """Какие из запрошенных id не нашлись: порядок запроса сохраняется."""

    @staticmethod
    def of(wanted: Sequence[int], found: set[int]) -> list[int]:
        missing: list[int] = []
        for record_id in wanted:
            if record_id not in found:
                missing.append(record_id)

        return missing


class SqlNames:
    """Подстановка идентификаторов схемы и таблиц в тексты SQL сущностей."""

    def __init__(self, schema: str) -> None:
        self._schema = schema

    @property
    def schema(self) -> str:
        return self._schema

    def render(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(  # type: ignore[arg-type]
            schema=sql.Identifier(self._schema),
            node=sql.Identifier(self._schema, DescriberTable.NODE.value),
            edge=sql.Identifier(self._schema, DescriberTable.EDGE.value),
        )


class SchemaSql:
    """DDL схемы: замок, схема, обе таблицы и связь между ними."""

    LOCK: ClassVar[sql.SQL] = sql.SQL("select pg_advisory_xact_lock(hashtext(%(key)s))")
    SCHEMA: ClassVar[str] = "create schema if not exists {schema}"
    TABLES: ClassVar[str] = """
create table if not exists {node} (
    id          bigserial primary key,
    scope_kind  varchar not null,
    scope_id    uuid not null,
    kind        varchar not null,
    address     jsonb not null,
    url_address varchar not null,
    description varchar not null,
    s__wrt_ts   timestamptz not null default now()
);
create unique index if not exists node_uk   on {node} (scope_id, address);
create index if not exists node_address_gin
    on {node} using gin (address jsonb_path_ops);
create index if not exists node_kind_btree  on {node} (kind);
create table if not exists {edge} (
    id          bigserial primary key,
    source_id   bigint not null references {node} on delete cascade,
    target_id   bigint not null references {node} on delete cascade,
    kind        varchar not null,
    description varchar not null,
    s__wrt_ts   timestamptz not null default now(),
    unique (source_id, target_id, kind)
)
"""


class DescriberSession:
    """Соединение с готовыми таблицами и имена SQL для таблиц сущностей."""

    def __init__(self, conn: psycopg.AsyncConnection[Any], names: SqlNames) -> None:
        self.conn = conn
        self.names = names


class DescriberStore:
    """Сессии хранилища: одно соединение на вызов инструмента, схема и
    таблицы готовы к первому запросу."""

    DDL_LOCK: ClassVar[str] = "boba.describer.ddl"

    def __init__(self, cfg: DescriberToolConfig) -> None:
        self._connection = cfg.connection
        self._names = SqlNames(cfg.db_schema)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[DescriberSession, None]:
        """Сессия с готовыми таблицами; соединение закрывается по выходу."""
        conn = await PayloadPostgres.connect_config(self._connection)
        async with conn:
            await self._ensure(conn)
            yield DescriberSession(conn, self._names)

    async def _ensure(self, conn: psycopg.AsyncConnection[Any]) -> None:
        """Схема и таблицы под одним advisory-замком: параллельные вызовы одного
        ответа модели иначе роняют create schema if not exists на уникальности
        pg_namespace. Без права на create schema её заводит администратор."""
        async with conn.transaction():
            await conn.execute(SchemaSql.LOCK, {"key": self.DDL_LOCK})

            try:
                async with conn.transaction():
                    await conn.execute(self._names.render(SchemaSql.SCHEMA))
            except InsufficientPrivilege:
                logger.info(
                    "no permission for create schema %r, assuming an administrator "
                    "created it",
                    self._names.schema,
                )

            await conn.execute(self._names.render(SchemaSql.TABLES))
