"""Общая часть хранилища описаний: конфиг, сессия, ключ области и создание
таблиц на старте плагина.

Сущности живут в своих модулях (nodes, edges) со своими SQL, моделями,
ошибками и инструментами; отсюда они берут только сессию — соединение и
сборщик запросов со схемой — и ключ области. Схему и обе таблицы создаёт
setup один раз при старте плагина: внешний потребитель забирает строки и
делает truncate, схему и таблицы не трогает.

Ошибки:
DescriberError — область вызова не годится ключом: id не uuid.
PostgresError — до базы приложения не достучаться или сервер отклонил файл
    схемы.
psycopg.Error — СУБД отклонила запрос.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from importlib.resources import files
from typing import Any, ClassVar
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from boba.db.postgres import PayloadPostgres, PgQueryBuilder, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.identity.context import Scope, ScopeKind
from boba.toolkit.manifest import PluginStorage
from boba.toolkit.types import SecretRevealing

logger = logging.getLogger(__name__)

__all__ = [
    "DescriberError",
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


class DescriberStore(PluginStorage):
    """Сессии хранилища: одно соединение на вызов инструмента. Таблицы node и
    edge создаёт setup файлами schema/*.sql пакета при старте плагина в
    процессе boba-mcp; реализация PluginStorage."""

    def __init__(self, cfg: DescriberToolConfig) -> None:
        self._connection = cfg.connection
        self._schema = PostgresSchema(cfg.db_schema)

    async def setup(self) -> None:
        """Схема и таблицы описаний на старте плагина; повтор безвреден."""
        schema_dir = files("boba.tool.describer") / "schema"
        conn = await PayloadPostgres.connect_config(self._connection)
        async with conn:
            await self._schema.apply_files(conn, schema_dir, {})

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[DescriberSession, None]:
        """Сессия на готовых таблицах; соединение закрывается по выходу."""
        conn = await PayloadPostgres.connect_config(self._connection)
        async with conn:
            yield DescriberSession(conn, self._schema)
