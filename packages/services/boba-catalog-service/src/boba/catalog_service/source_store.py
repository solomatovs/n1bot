"""Хранилище источников метаданных в Postgres: источники, привязки
подключений, версии со снимками в родной структуре (по таблице на род
записи, полная копия на версию), записи синхронизаций, черновики ручных
источников с порциями операций.

Таблицы снимков описаны спецификациями SnapshotTable: одна и та же
спецификация даёт DDL, вставку строк версии и чтение версии обратно в модели
домена. Запись версии — одна транзакция: номер версии, шапка, все строки.

Ошибки:
CatalogStoreError — Postgres недоступен, ответ битый, строки не складываются
    в снимок.
SourceNotFoundError — источника с таким id нет.
SourceVersionNotFoundError — у источника нет такой версии.
SourceDraftNotFoundError — черновика ручного источника нет.
SourceNotManualError — черновики открыты только ручному источнику.
DraftClosedError — черновик уже опубликован или отброшен.
DraftConflictError — expected_seq не равен последнему seq черновика.
DraftStaleError — base_version черновика отстал от версии источника.
SourceOpError — порция не применима к снимку черновика.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterator, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any, ClassVar, LiteralString, TypeVar
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from boba.catalog import (
    ChColumn,
    ChDatabase,
    ChDictionary,
    ChDictionaryAttribute,
    ChSnapshot,
    ChTable,
    PgColumn,
    PgConstraint,
    PgDatabase,
    PgIndex,
    PgRelation,
    PgRoutine,
    PgRoutineArg,
    PgSchema,
    PgSequence,
    PgSnapshot,
    PgType,
    SourceDiff,
    SourceKind,
    SourceOperationList,
    SourceRecord,
    SourceSnapshot,
)
from boba.catalog_service.config import CatalogConfig
from boba.catalog_service.records import (
    CatalogStoreError,
    DraftAuthor,
    DraftClosedError,
    DraftConflictError,
    DraftStaleError,
    DraftStatus,
    Source,
    SourceConnection,
    SourceDraft,
    SourceDraftNotFoundError,
    SourceDraftOp,
    SourceDraftState,
    SourceNotFoundError,
    SourceNotManualError,
    SourceSpec,
    SourceVersion,
    SourceVersionNotFoundError,
    VersionOrigin,
)
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresTable, SqlNames

logger = logging.getLogger(__name__)

__all__ = ["SourceStore", "SourceTable"]

Cursor = psycopg.AsyncCursor[DictRow]
ModelT = TypeVar("ModelT", bound=BaseModel)


class SourceTable(StrEnum):
    """Таблицы источников в схеме каталога."""

    SOURCES = "sources"
    SOURCE_CONNECTIONS = "source_connections"
    SOURCE_VERSIONS = "source_versions"
    SYNCS = "syncs"
    SOURCE_DRAFTS = "source_drafts"
    SOURCE_DRAFT_OPS = "source_draft_ops"
    PG_DATABASES = "pg_databases"
    PG_SCHEMAS = "pg_schemas"
    PG_RELATIONS = "pg_relations"
    PG_COLUMNS = "pg_columns"
    PG_CONSTRAINTS = "pg_constraints"
    PG_INDEXES = "pg_indexes"
    PG_ROUTINES = "pg_routines"
    PG_ROUTINE_ARGS = "pg_routine_args"
    PG_SEQUENCES = "pg_sequences"
    PG_TYPES = "pg_types"
    CH_DATABASES = "ch_databases"
    CH_TABLES = "ch_tables"
    CH_COLUMNS = "ch_columns"
    CH_DICTIONARIES = "ch_dictionaries"
    CH_DICTIONARY_ATTRIBUTES = "ch_dictionary_attributes"


class SqlType(StrEnum):
    """Типы колонок таблиц снимков."""

    TEXT = "text"
    TEXT_NULL = "text null"
    INT = "integer"
    INT_NULL = "integer null"
    BIGINT = "bigint"
    BIGINT_NULL = "bigint null"
    REAL = "real"
    REAL_NULL = "real null"
    BOOL = "boolean"
    BOOL_NULL = "boolean null"
    TEXTS = "text[]"
    TEXTS_NULL = "text[] null"
    JSONB = "jsonb"
    JSONB_NULL = "jsonb null"

    @property
    def is_json(self) -> bool:
        return self in (SqlType.JSONB, SqlType.JSONB_NULL)


class SnapshotColumn:
    """Колонка таблицы снимка: имя поля модели, имя колонки, тип."""

    def __init__(self, field: str, sql_type: SqlType, column: str = "") -> None:
        self.field = field
        self.sql_type = sql_type
        self.column = column or field


class SnapshotTable:
    """Таблица одного рода записей снимка: модель, колонки, родной ключ."""

    def __init__(
        self,
        table: SourceTable,
        model: type[SourceRecord],
        columns: Sequence[SnapshotColumn],
        key: Sequence[str],
    ) -> None:
        self.table = table
        self.model = model
        self.columns = tuple(columns)
        self.key = tuple(key)

    def column_of(self, field: str) -> str:
        for column in self.columns:
            if column.field == field:
                return column.column

        msg = f"snapshot table {self.table.value}: no field {field}"
        raise CatalogStoreError(msg)


class SnapshotTables:
    """Спецификации всех таблиц снимков по видам источников; порядок — от
    родителей к детям, чтобы вставка шла в порядке зависимостей."""

    PG_HEAD: ClassVar[tuple[SnapshotColumn, ...]] = (
        SnapshotColumn("database", SqlType.TEXT),
        SnapshotColumn("schema_name", SqlType.TEXT, "schema"),
    )

    POSTGRES: ClassVar[tuple[SnapshotTable, ...]] = (
        SnapshotTable(
            SourceTable.PG_DATABASES,
            PgDatabase,
            (
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("owner", SqlType.TEXT),
                SnapshotColumn("encoding", SqlType.TEXT),
                SnapshotColumn("collate", SqlType.TEXT),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
            ),
            ("name",),
        ),
        SnapshotTable(
            SourceTable.PG_SCHEMAS,
            PgSchema,
            (
                SnapshotColumn("database", SqlType.TEXT),
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("owner", SqlType.TEXT),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
            ),
            ("database", "name"),
        ),
        SnapshotTable(
            SourceTable.PG_RELATIONS,
            PgRelation,
            (
                *PG_HEAD,
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("kind", SqlType.TEXT),
                SnapshotColumn("owner", SqlType.TEXT),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
                SnapshotColumn("tablespace", SqlType.TEXT_NULL),
                SnapshotColumn("persistence", SqlType.TEXT),
                SnapshotColumn("row_estimate", SqlType.BIGINT),
                SnapshotColumn("total_bytes", SqlType.BIGINT),
                SnapshotColumn("partition_key", SqlType.TEXT_NULL),
                SnapshotColumn("partition_of", SqlType.TEXT_NULL),
                SnapshotColumn("partition_bound", SqlType.TEXT_NULL),
                SnapshotColumn("definition", SqlType.TEXT_NULL),
                SnapshotColumn("check_option", SqlType.TEXT_NULL),
                SnapshotColumn("populated", SqlType.BOOL_NULL),
                SnapshotColumn("foreign_server", SqlType.TEXT_NULL),
                SnapshotColumn("options", SqlType.JSONB),
            ),
            ("database", "schema", "name"),
        ),
        SnapshotTable(
            SourceTable.PG_COLUMNS,
            PgColumn,
            (
                *PG_HEAD,
                SnapshotColumn("relation", SqlType.TEXT),
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("ordinal", SqlType.INT),
                SnapshotColumn("type", SqlType.TEXT),
                SnapshotColumn("nullable", SqlType.BOOL),
                SnapshotColumn("default", SqlType.TEXT_NULL),
                SnapshotColumn("identity", SqlType.TEXT_NULL),
                SnapshotColumn("generated", SqlType.TEXT_NULL),
                SnapshotColumn("collation", SqlType.TEXT_NULL),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
            ),
            ("database", "schema", "relation", "name"),
        ),
        SnapshotTable(
            SourceTable.PG_CONSTRAINTS,
            PgConstraint,
            (
                *PG_HEAD,
                SnapshotColumn("relation", SqlType.TEXT),
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("kind", SqlType.TEXT),
                SnapshotColumn("columns", SqlType.TEXTS),
                SnapshotColumn("ref_schema", SqlType.TEXT_NULL),
                SnapshotColumn("ref_relation", SqlType.TEXT_NULL),
                SnapshotColumn("ref_columns", SqlType.TEXTS_NULL),
                SnapshotColumn("on_update", SqlType.TEXT_NULL),
                SnapshotColumn("on_delete", SqlType.TEXT_NULL),
                SnapshotColumn("deferrable", SqlType.BOOL),
                SnapshotColumn("initially_deferred", SqlType.BOOL),
                SnapshotColumn("definition", SqlType.TEXT),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
            ),
            ("database", "schema", "relation", "name"),
        ),
        SnapshotTable(
            SourceTable.PG_INDEXES,
            PgIndex,
            (
                *PG_HEAD,
                SnapshotColumn("relation", SqlType.TEXT),
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("method", SqlType.TEXT),
                SnapshotColumn("unique", SqlType.BOOL),
                SnapshotColumn("primary", SqlType.BOOL),
                SnapshotColumn("columns", SqlType.TEXTS),
                SnapshotColumn("predicate", SqlType.TEXT_NULL),
                SnapshotColumn("definition", SqlType.TEXT),
                SnapshotColumn("total_bytes", SqlType.BIGINT),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
            ),
            ("database", "schema", "relation", "name"),
        ),
        SnapshotTable(
            SourceTable.PG_ROUTINES,
            PgRoutine,
            (
                *PG_HEAD,
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("signature", SqlType.TEXT),
                SnapshotColumn("kind", SqlType.TEXT),
                SnapshotColumn("owner", SqlType.TEXT),
                SnapshotColumn("language", SqlType.TEXT),
                SnapshotColumn("arguments", SqlType.TEXT),
                SnapshotColumn("returns", SqlType.TEXT_NULL),
                SnapshotColumn("returns_set", SqlType.BOOL),
                SnapshotColumn("volatility", SqlType.TEXT),
                SnapshotColumn("strict", SqlType.BOOL),
                SnapshotColumn("security_definer", SqlType.BOOL),
                SnapshotColumn("parallel", SqlType.TEXT),
                SnapshotColumn("cost", SqlType.REAL),
                SnapshotColumn("rows", SqlType.REAL_NULL),
                SnapshotColumn("body", SqlType.TEXT),
                SnapshotColumn("definition", SqlType.TEXT),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
            ),
            ("database", "schema", "name", "signature"),
        ),
        SnapshotTable(
            SourceTable.PG_ROUTINE_ARGS,
            PgRoutineArg,
            (
                *PG_HEAD,
                SnapshotColumn("routine", SqlType.TEXT),
                SnapshotColumn("signature", SqlType.TEXT),
                SnapshotColumn("position", SqlType.INT),
                SnapshotColumn("name", SqlType.TEXT_NULL),
                SnapshotColumn("type", SqlType.TEXT),
                SnapshotColumn("mode", SqlType.TEXT),
                SnapshotColumn("default", SqlType.TEXT_NULL),
            ),
            ("database", "schema", "routine", "signature", "position"),
        ),
        SnapshotTable(
            SourceTable.PG_SEQUENCES,
            PgSequence,
            (
                *PG_HEAD,
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("type", SqlType.TEXT),
                SnapshotColumn("start", SqlType.BIGINT),
                SnapshotColumn("minimum", SqlType.BIGINT),
                SnapshotColumn("maximum", SqlType.BIGINT),
                SnapshotColumn("increment", SqlType.BIGINT),
                SnapshotColumn("cycle", SqlType.BOOL),
                SnapshotColumn("cache", SqlType.BIGINT),
                SnapshotColumn("last_value", SqlType.BIGINT_NULL),
                SnapshotColumn("owned_by", SqlType.TEXT_NULL),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
            ),
            ("database", "schema", "name"),
        ),
        SnapshotTable(
            SourceTable.PG_TYPES,
            PgType,
            (
                *PG_HEAD,
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("kind", SqlType.TEXT),
                SnapshotColumn("owner", SqlType.TEXT),
                SnapshotColumn("labels", SqlType.TEXTS_NULL),
                SnapshotColumn("base_type", SqlType.TEXT_NULL),
                SnapshotColumn("constraint", SqlType.TEXT_NULL),
                SnapshotColumn("attributes", SqlType.JSONB_NULL),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
            ),
            ("database", "schema", "name"),
        ),
    )

    CLICKHOUSE: ClassVar[tuple[SnapshotTable, ...]] = (
        SnapshotTable(
            SourceTable.CH_DATABASES,
            ChDatabase,
            (
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("engine", SqlType.TEXT),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
            ),
            ("name",),
        ),
        SnapshotTable(
            SourceTable.CH_TABLES,
            ChTable,
            (
                SnapshotColumn("database", SqlType.TEXT),
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("kind", SqlType.TEXT),
                SnapshotColumn("engine", SqlType.TEXT),
                SnapshotColumn("engine_full", SqlType.TEXT),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
                SnapshotColumn("partition_key", SqlType.TEXT_NULL),
                SnapshotColumn("sorting_key", SqlType.TEXT_NULL),
                SnapshotColumn("primary_key", SqlType.TEXT_NULL),
                SnapshotColumn("sampling_key", SqlType.TEXT_NULL),
                SnapshotColumn("ttl", SqlType.TEXT_NULL),
                SnapshotColumn("settings", SqlType.JSONB),
                SnapshotColumn("definition", SqlType.TEXT_NULL),
                SnapshotColumn("target", SqlType.TEXT_NULL),
                SnapshotColumn("dependencies", SqlType.TEXTS),
                SnapshotColumn("total_rows", SqlType.BIGINT_NULL),
                SnapshotColumn("total_bytes", SqlType.BIGINT_NULL),
                SnapshotColumn("metadata_modified_at", SqlType.TEXT),
                SnapshotColumn("create_query", SqlType.TEXT),
            ),
            ("database", "name"),
        ),
        SnapshotTable(
            SourceTable.CH_COLUMNS,
            ChColumn,
            (
                SnapshotColumn("database", SqlType.TEXT),
                SnapshotColumn("table", SqlType.TEXT),
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("position", SqlType.INT),
                SnapshotColumn("type", SqlType.TEXT),
                SnapshotColumn("default_kind", SqlType.TEXT_NULL),
                SnapshotColumn("default_expression", SqlType.TEXT_NULL),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
                SnapshotColumn("codec", SqlType.TEXT_NULL),
                SnapshotColumn("ttl", SqlType.TEXT_NULL),
                SnapshotColumn("in_partition_key", SqlType.BOOL),
                SnapshotColumn("in_sorting_key", SqlType.BOOL),
                SnapshotColumn("in_primary_key", SqlType.BOOL),
                SnapshotColumn("in_sampling_key", SqlType.BOOL),
            ),
            ("database", "table", "name"),
        ),
        SnapshotTable(
            SourceTable.CH_DICTIONARIES,
            ChDictionary,
            (
                SnapshotColumn("database", SqlType.TEXT),
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("status", SqlType.TEXT),
                SnapshotColumn("layout", SqlType.TEXT),
                SnapshotColumn("source", SqlType.TEXT),
                SnapshotColumn("key_columns", SqlType.TEXTS),
                SnapshotColumn("lifetime_min", SqlType.INT),
                SnapshotColumn("lifetime_max", SqlType.INT),
                SnapshotColumn("comment", SqlType.TEXT_NULL),
                SnapshotColumn("create_query", SqlType.TEXT),
            ),
            ("database", "name"),
        ),
        SnapshotTable(
            SourceTable.CH_DICTIONARY_ATTRIBUTES,
            ChDictionaryAttribute,
            (
                SnapshotColumn("database", SqlType.TEXT),
                SnapshotColumn("dictionary", SqlType.TEXT),
                SnapshotColumn("name", SqlType.TEXT),
                SnapshotColumn("position", SqlType.INT),
                SnapshotColumn("type", SqlType.TEXT),
            ),
            ("database", "dictionary", "name"),
        ),
    )

    @classmethod
    def of_kind(cls, kind: SourceKind) -> tuple[SnapshotTable, ...]:
        if kind is SourceKind.POSTGRES:
            return cls.POSTGRES

        return cls.CLICKHOUSE

    @classmethod
    def all(cls) -> Iterator[SnapshotTable]:
        yield from cls.POSTGRES
        yield from cls.CLICKHOUSE


class SourcesColumn(StrEnum):
    ID = "id"
    KIND = "kind"
    NAME = "name"
    DESCRIPTION = "description"
    MANUAL = "manual"
    CREATED_BY = "created_by"
    CREATED_AT = "created_at"


class SourceConnectionsColumn(StrEnum):
    SOURCE_ID = "source_id"
    CONNECTION_ID = "connection_id"
    BOUND_BY = "bound_by"
    BOUND_AT = "bound_at"


class SourceVersionsColumn(StrEnum):
    SOURCE_ID = "source_id"
    VERSION = "version"
    TAKEN_AT = "taken_at"
    TAKEN_BY = "taken_by"
    CONNECTION_ID = "connection_id"
    SYNC_ID = "sync_id"
    OBJECTS_TOTAL = "objects_total"
    SERVER_VERSION = "server_version"


class SyncsColumn(StrEnum):
    ID = "id"
    SOURCE_ID = "source_id"
    CONNECTION_ID = "connection_id"
    STARTED_BY = "started_by"
    STARTED_AT = "started_at"
    FINISHED_AT = "finished_at"
    STATUS = "status"
    SCOPE = "scope"
    OBJECTS_TOTAL = "objects_total"
    OBJECTS_DONE = "objects_done"
    ERROR = "error"
    VERSION = "version"


class SourceDraftsColumn(StrEnum):
    ID = "id"
    SOURCE_ID = "source_id"
    NAME = "name"
    BASE_VERSION = "base_version"
    STATUS = "status"
    CREATED_BY = "created_by"
    CREATED_AT = "created_at"
    CLOSED_AT = "closed_at"


class SourceDraftOpsColumn(StrEnum):
    DRAFT_ID = "draft_id"
    SEQ = "seq"
    AUTHOR_ID = "author_id"
    VIA = "via"
    OPERATIONS = "operations"
    CREATED_AT = "created_at"


class SnapshotKey(StrEnum):
    """Служебные колонки каждой таблицы снимка."""

    SOURCE_ID = "source_id"
    VERSION = "version"


class SourceStore(PostgresTable):
    """Хранилище источников: живёт под CatalogService рядом с CatalogStore в
    той же схеме; прав не знает."""

    def __init__(
        self, cfg: CatalogConfig, pool: AsyncPostgresPool | None = None
    ) -> None:
        postgres = cfg.connection
        if pool is None:
            postgres = cfg.require_conn()

        super().__init__(postgres, cfg.db_schema, pool)
        self._cfg = cfg

    def _sql(self, text: LiteralString) -> sql.Composed:
        """SQL с именами таблиц по значению enum и колонок с префиксом:
        s_ sources, sc_ source_connections, sv_ source_versions, sy_ syncs,
        sd_ source_drafts, so_ source_draft_ops."""
        names: dict[str, sql.Composable] = {}
        for table in SourceTable:
            names[table.value] = self._table(table)

        prefixed: dict[str, type[StrEnum]] = {
            "s": SourcesColumn,
            "sc": SourceConnectionsColumn,
            "sv": SourceVersionsColumn,
            "sy": SyncsColumn,
            "sd": SourceDraftsColumn,
            "so": SourceDraftOpsColumn,
        }
        for prefix, columns in prefixed.items():
            for column in columns:
                names[f"{prefix}_{column.value}"] = SqlNames.ident(column)

        return sql.SQL(text).format(**names)

    @asynccontextmanager
    async def _guarded(self, action: str) -> AsyncGenerator[None]:
        try:
            yield
        except (psycopg.Error, PostgresError) as exc:
            msg = f"catalog sources: {action} failed"
            raise CatalogStoreError(msg) from exc

    @asynccontextmanager
    async def _transaction(self, action: str) -> AsyncGenerator[Cursor]:
        pool = await self._pool()
        async with (
            self._guarded(action),
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            yield cur

    async def setup(self) -> None:
        """Схема и таблицы; повтор безвреден."""
        async with self._guarded("setup"):
            await self._apply_ddl(self._ddl())

        logger.info("catalog sources ready: %s", self._cfg.db_schema)

    def _ddl(self) -> tuple[sql.Composed, ...]:
        statements: list[sql.Composed] = [
            self._sql(
                """
                create table if not exists {sources} (
                    {s_id}          uuid primary key,
                    {s_kind}        text not null,
                    {s_name}        text not null unique,
                    {s_description} text not null default '',
                    {s_manual}      boolean not null default false,
                    {s_created_by}  uuid not null,
                    {s_created_at}  timestamptz not null default now()
                )
                """
            ),
            self._sql(
                """
                create table if not exists {source_connections} (
                    {sc_source_id}     uuid not null references {sources} ({s_id})
                                       on delete cascade,
                    {sc_connection_id} uuid not null,
                    {sc_bound_by}      uuid not null,
                    {sc_bound_at}      timestamptz not null default now(),
                    primary key ({sc_source_id}, {sc_connection_id})
                )
                """
            ),
            self._sql(
                """
                create table if not exists {syncs} (
                    {sy_id}            uuid primary key,
                    {sy_source_id}     uuid not null references {sources} ({s_id})
                                       on delete cascade,
                    {sy_connection_id} uuid not null,
                    {sy_started_by}    uuid not null,
                    {sy_started_at}    timestamptz not null default now(),
                    {sy_finished_at}   timestamptz null,
                    {sy_status}        text not null,
                    {sy_scope}         jsonb not null default '{{}}'::jsonb,
                    {sy_objects_total} integer null,
                    {sy_objects_done}  integer not null default 0,
                    {sy_error}         text null,
                    {sy_version}       integer null
                )
                """
            ),
            self._sql(
                """
                create table if not exists {source_versions} (
                    {sv_source_id}      uuid not null references {sources} ({s_id})
                                        on delete cascade,
                    {sv_version}        integer not null,
                    {sv_taken_at}       timestamptz not null default now(),
                    {sv_taken_by}       uuid not null,
                    {sv_connection_id}  uuid null,
                    {sv_sync_id}        uuid null references {syncs} ({sy_id}),
                    {sv_objects_total}  integer not null default 0,
                    {sv_server_version} text null,
                    primary key ({sv_source_id}, {sv_version})
                )
                """
            ),
            self._sql(
                """
                create table if not exists {source_drafts} (
                    {sd_id}           uuid primary key,
                    {sd_source_id}    uuid not null references {sources} ({s_id})
                                      on delete cascade,
                    {sd_name}         text not null,
                    {sd_base_version} integer not null,
                    {sd_status}       text not null,
                    {sd_created_by}   uuid not null,
                    {sd_created_at}   timestamptz not null default now(),
                    {sd_closed_at}    timestamptz null
                )
                """
            ),
            self._sql(
                """
                create table if not exists {source_draft_ops} (
                    {so_draft_id}   uuid not null references {source_drafts} ({sd_id})
                                    on delete cascade,
                    {so_seq}        integer not null,
                    {so_author_id}  uuid not null,
                    {so_via}        text not null,
                    {so_operations} jsonb not null,
                    {so_created_at} timestamptz not null default now(),
                    primary key ({so_draft_id}, {so_seq})
                )
                """
            ),
        ]
        for spec in SnapshotTables.all():
            statements.append(self._snapshot_ddl(spec))

        return tuple(statements)

    def _snapshot_ddl(self, spec: SnapshotTable) -> sql.Composed:
        definitions: list[sql.Composable] = [
            sql.SQL("{} uuid not null references {} ({}) on delete cascade").format(
                sql.Identifier(SnapshotKey.SOURCE_ID.value),
                self._table(SourceTable.SOURCES),
                sql.Identifier(SourcesColumn.ID.value),
            ),
            sql.SQL("{} integer not null").format(
                sql.Identifier(SnapshotKey.VERSION.value)
            ),
        ]
        for column in spec.columns:
            definitions.append(
                sql.SQL("{} {}").format(
                    sql.Identifier(column.column), sql.SQL(column.sql_type.value)
                )
            )

        key: list[sql.Composable] = [
            sql.Identifier(SnapshotKey.SOURCE_ID.value),
            sql.Identifier(SnapshotKey.VERSION.value),
        ]
        for name in spec.key:
            key.append(sql.Identifier(name))

        definitions.append(sql.SQL("primary key ({})").format(sql.SQL(", ").join(key)))

        return sql.SQL("create table if not exists {} ({})").format(
            self._table(spec.table), sql.SQL(", ").join(definitions)
        )

    # --- источники ---

    async def create_source(self, spec: SourceSpec, created_by: UUID) -> Source:
        source_id = uuid4()
        async with self._transaction("create source") as cur:
            await cur.execute(
                self._sql(
                    """
                    insert into {sources}
                        ({s_id}, {s_kind}, {s_name}, {s_description}, {s_manual},
                         {s_created_by})
                    values
                        (%(id)s, %(kind)s, %(name)s, %(description)s, %(manual)s,
                         %(created_by)s)
                    """
                ),
                {
                    "id": source_id,
                    "kind": spec.kind.value,
                    "name": spec.name,
                    "description": spec.description,
                    "manual": spec.manual,
                    "created_by": created_by,
                },
            )
            return await self._source(cur, source_id)

    async def get_source(self, source_id: UUID) -> Source:
        async with self._transaction("get source") as cur:
            return await self._source(cur, source_id)

    async def list_sources(self) -> Sequence[Source]:
        async with self._transaction("list sources") as cur:
            await cur.execute(self._source_select(" order by s.{s_name}"))
            rows = await cur.fetchall()

        sources: list[Source] = []
        for row in rows:
            sources.append(self._source_of(row))

        return sources

    async def update_source(self, source_id: UUID, spec: SourceSpec) -> Source:
        async with self._transaction("update source") as cur:
            await self._source(cur, source_id)
            await cur.execute(
                self._sql(
                    """
                    update {sources}
                    set {s_name} = %(name)s,
                        {s_description} = %(description)s,
                        {s_manual} = %(manual)s
                    where {s_id} = %(id)s
                    """
                ),
                {
                    "id": source_id,
                    "name": spec.name,
                    "description": spec.description,
                    "manual": spec.manual,
                },
            )
            return await self._source(cur, source_id)

    async def delete_source(self, source_id: UUID) -> bool:
        async with self._transaction("delete source") as cur:
            await cur.execute(
                self._sql("delete from {sources} where {s_id} = %(id)s"),
                {"id": source_id},
            )
            return cur.rowcount > 0

    # --- подключения ---

    async def bind_connection(
        self, source_id: UUID, connection_id: UUID, bound_by: UUID
    ) -> SourceConnection:
        async with self._transaction("bind connection") as cur:
            await self._source(cur, source_id)
            await cur.execute(
                self._sql(
                    """
                    insert into {source_connections}
                        ({sc_source_id}, {sc_connection_id}, {sc_bound_by})
                    values (%(source_id)s, %(connection_id)s, %(bound_by)s)
                    on conflict do nothing
                    """
                ),
                {
                    "source_id": source_id,
                    "connection_id": connection_id,
                    "bound_by": bound_by,
                },
            )
            await cur.execute(
                self._sql(
                    """
                    select {sc_source_id}, {sc_connection_id}, {sc_bound_by},
                           {sc_bound_at}
                    from {source_connections}
                    where {sc_source_id} = %(source_id)s
                      and {sc_connection_id} = %(connection_id)s
                    """
                ),
                {"source_id": source_id, "connection_id": connection_id},
            )
            row = await cur.fetchone()

        if row is None:
            msg = "catalog sources: binding vanished"
            raise CatalogStoreError(msg)

        return self._parse(SourceConnection, dict(row))

    async def unbind_connection(self, source_id: UUID, connection_id: UUID) -> bool:
        async with self._transaction("unbind connection") as cur:
            await cur.execute(
                self._sql(
                    """
                    delete from {source_connections}
                    where {sc_source_id} = %(source_id)s
                      and {sc_connection_id} = %(connection_id)s
                    """
                ),
                {"source_id": source_id, "connection_id": connection_id},
            )
            return cur.rowcount > 0

    async def connections_of(self, source_id: UUID) -> Sequence[SourceConnection]:
        async with self._transaction("connections of source") as cur:
            await cur.execute(
                self._sql(
                    """
                    select {sc_source_id}, {sc_connection_id}, {sc_bound_by},
                           {sc_bound_at}
                    from {source_connections}
                    where {sc_source_id} = %(source_id)s
                    order by {sc_bound_at}
                    """
                ),
                {"source_id": source_id},
            )
            rows = await cur.fetchall()

        bound: list[SourceConnection] = []
        for row in rows:
            bound.append(self._parse(SourceConnection, dict(row)))

        return bound

    # --- версии ---

    async def write_version(
        self, source_id: UUID, snapshot: SourceSnapshot, origin: VersionOrigin
    ) -> SourceVersion:
        """Новая версия источника целиком одной транзакцией. Этим же путём
        синхронизация переносит staging в хранилище."""
        snapshot.check()
        async with self._transaction("write version") as cur:
            source = await self._source(cur, source_id, lock=True)
            self._require_kind(source, snapshot)
            version = source.latest_version + 1
            await cur.execute(
                self._sql(
                    """
                    insert into {source_versions}
                        ({sv_source_id}, {sv_version}, {sv_taken_by},
                         {sv_connection_id}, {sv_sync_id}, {sv_objects_total},
                         {sv_server_version})
                    values
                        (%(source_id)s, %(version)s, %(taken_by)s, %(connection_id)s,
                         %(sync_id)s, %(objects_total)s, %(server_version)s)
                    """
                ),
                {
                    "source_id": source_id,
                    "version": version,
                    "taken_by": origin.taken_by,
                    "connection_id": origin.connection_id,
                    "sync_id": origin.sync_id,
                    "objects_total": snapshot.objects_count(),
                    "server_version": origin.server_version,
                },
            )
            await self._insert_snapshot(cur, source_id, version, snapshot)
            return await self._version(cur, source_id, version)

    async def versions_of(self, source_id: UUID) -> Sequence[SourceVersion]:
        async with self._transaction("versions of source") as cur:
            await self._source(cur, source_id)
            await cur.execute(
                self._sql(
                    """
                    select {sv_source_id}, {sv_version}, {sv_taken_at}, {sv_taken_by},
                           {sv_connection_id}, {sv_sync_id}, {sv_objects_total},
                           {sv_server_version}
                    from {source_versions}
                    where {sv_source_id} = %(source_id)s
                    order by {sv_version}
                    """
                ),
                {"source_id": source_id},
            )
            rows = await cur.fetchall()

        versions: list[SourceVersion] = []
        for row in rows:
            versions.append(self._parse(SourceVersion, dict(row)))

        return versions

    async def version_of(self, source_id: UUID, version: int) -> SourceVersion:
        async with self._transaction("version of source") as cur:
            return await self._version(cur, source_id, version)

    async def snapshot_of(self, source_id: UUID, version: int) -> SourceSnapshot:
        """Снимок версии; версия 0 — пустой снимок вида источника."""
        async with self._transaction("snapshot of source") as cur:
            source = await self._source(cur, source_id)
            if version == 0:
                return self._empty(source.kind)

            await self._version(cur, source_id, version)
            return await self._read_snapshot(cur, source, version)

    async def latest_snapshot(self, source_id: UUID) -> SourceSnapshot:
        async with self._transaction("latest snapshot") as cur:
            source = await self._source(cur, source_id)
            if source.latest_version == 0:
                return self._empty(source.kind)

            return await self._read_snapshot(cur, source, source.latest_version)

    async def diff_of(self, source_id: UUID, old: int, new: int) -> SourceDiff:
        before = await self.snapshot_of(source_id, old)
        after = await self.snapshot_of(source_id, new)
        return SourceDiff.between(source_id, before, after)

    # --- черновики ручного источника ---

    async def create_draft(
        self, source_id: UUID, name: str, created_by: UUID
    ) -> SourceDraft:
        async with self._transaction("create source draft") as cur:
            source = await self._source(cur, source_id)
            if not source.manual:
                raise SourceNotManualError(source_id)

            draft_id = uuid4()
            await cur.execute(
                self._sql(
                    """
                    insert into {source_drafts}
                        ({sd_id}, {sd_source_id}, {sd_name}, {sd_base_version},
                         {sd_status}, {sd_created_by})
                    values
                        (%(id)s, %(source_id)s, %(name)s, %(base_version)s,
                         %(status)s, %(created_by)s)
                    """
                ),
                {
                    "id": draft_id,
                    "source_id": source_id,
                    "name": name,
                    "base_version": source.latest_version,
                    "status": DraftStatus.OPEN.value,
                    "created_by": created_by,
                },
            )
            return await self._draft(cur, draft_id, lock=False)

    async def get_draft(self, draft_id: UUID) -> SourceDraft:
        async with self._transaction("get source draft") as cur:
            return await self._draft(cur, draft_id, lock=False)

    async def open_drafts(self, source_id: UUID) -> Sequence[SourceDraft]:
        async with self._transaction("open source drafts") as cur:
            await cur.execute(
                self._sql(
                    """
                    select {sd_id}, {sd_source_id}, {sd_name}, {sd_base_version},
                           {sd_status}, {sd_created_by}, {sd_created_at}
                    from {source_drafts}
                    where {sd_source_id} = %(source_id)s
                      and {sd_status} = %(status)s
                    order by {sd_created_at}
                    """
                ),
                {"source_id": source_id, "status": DraftStatus.OPEN.value},
            )
            rows = await cur.fetchall()

        drafts: list[SourceDraft] = []
        for row in rows:
            drafts.append(self._parse(SourceDraft, dict(row)))

        return drafts

    async def discard_draft(self, draft_id: UUID) -> SourceDraft:
        async with self._transaction("discard source draft") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)
            return await self._set_status(cur, draft_id, DraftStatus.DISCARDED)

    async def draft_state(self, draft_id: UUID) -> SourceDraftState:
        async with self._transaction("source draft state") as cur:
            draft = await self._draft(cur, draft_id, lock=False)
            source = await self._source(cur, draft.source_id)
            base = await self._base_snapshot(cur, source, draft.base_version)
            ops = await self._ops_of(cur, draft_id)
            snapshot = self._fold(base, ops)
            seq = 0
            if ops:
                seq = ops[-1].seq

            return SourceDraftState(
                draft=draft,
                snapshot=snapshot,
                diff=SourceDiff.between(source.id, base, snapshot),
                seq=seq,
            )

    async def append_ops(
        self,
        draft_id: UUID,
        expected_seq: int,
        operations: SourceOperationList,
        author: DraftAuthor,
    ) -> SourceDraftState:
        """Порция операций с проверкой номера: seq черновика под блокировкой.

        Ошибки:
        DraftConflictError — expected_seq отстал.
        SourceOpError — порция не применима к текущему снимку черновика.
        """
        async with self._transaction("append source ops") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)
            source = await self._source(cur, draft.source_id)
            ops = await self._ops_of(cur, draft_id)
            current_seq = 0
            if ops:
                current_seq = ops[-1].seq

            if current_seq != expected_seq:
                raise DraftConflictError(draft_id, expected_seq, current_seq)

            base = await self._base_snapshot(cur, source, draft.base_version)
            folded = self._fold(base, ops)
            snapshot = operations.apply(folded)

            await cur.execute(
                self._sql(
                    """
                    insert into {source_draft_ops}
                        ({so_draft_id}, {so_seq}, {so_author_id}, {so_via},
                         {so_operations})
                    values (%(draft_id)s, %(seq)s, %(author_id)s, %(via)s,
                            %(operations)s)
                    """
                ),
                {
                    "draft_id": draft_id,
                    "seq": current_seq + 1,
                    "author_id": author.user_id,
                    "via": author.via.value,
                    "operations": Jsonb(operations.model_dump(mode="json")),
                },
            )

            return SourceDraftState(
                draft=draft,
                snapshot=snapshot,
                diff=SourceDiff.between(source.id, base, snapshot),
                seq=current_seq + 1,
            )

    async def publish_draft(self, draft_id: UUID, author: DraftAuthor) -> SourceVersion:
        """Свёрнутый снимок черновика становится новой версией источника.

        Ошибки:
        DraftStaleError — источник ушёл вперёд после base_version.
        """
        async with self._transaction("publish source draft") as cur:
            draft = await self._draft(cur, draft_id, lock=True)
            self._require_open(draft)
            source = await self._source(cur, draft.source_id, lock=True)
            if source.latest_version != draft.base_version:
                raise DraftStaleError(
                    draft_id, draft.base_version, source.latest_version
                )

            base = await self._base_snapshot(cur, source, draft.base_version)
            ops = await self._ops_of(cur, draft_id)
            snapshot = self._fold(base, ops)
            snapshot.check()
            version = source.latest_version + 1
            await cur.execute(
                self._sql(
                    """
                    insert into {source_versions}
                        ({sv_source_id}, {sv_version}, {sv_taken_by},
                         {sv_objects_total})
                    values (%(source_id)s, %(version)s, %(taken_by)s,
                            %(objects_total)s)
                    """
                ),
                {
                    "source_id": source.id,
                    "version": version,
                    "taken_by": author.user_id,
                    "objects_total": snapshot.objects_count(),
                },
            )
            await self._insert_snapshot(cur, source.id, version, snapshot)
            await self._set_status(cur, draft_id, DraftStatus.PUBLISHED)
            return await self._version(cur, source.id, version)

    # --- внутреннее: источники и версии ---

    SOURCE_SELECT: ClassVar[LiteralString] = """
        select s.{s_id}, s.{s_kind}, s.{s_name}, s.{s_description}, s.{s_manual},
               s.{s_created_by}, s.{s_created_at},
               coalesce((select max(v.{sv_version}) from {source_versions} v
                         where v.{sv_source_id} = s.{s_id}), 0) as latest_version
        from {sources} s
        """

    def _source_select(self, tail: LiteralString) -> sql.Composed:
        return sql.Composed([self._sql(self.SOURCE_SELECT), self._sql(tail)])

    async def _source(
        self, cur: Cursor, source_id: UUID, *, lock: bool = False
    ) -> Source:
        tail: LiteralString = " where s.{s_id} = %(id)s"
        if lock:
            tail = " where s.{s_id} = %(id)s for update"

        await cur.execute(self._source_select(tail), {"id": source_id})
        row = await cur.fetchone()
        if row is None:
            raise SourceNotFoundError(source_id)

        return self._source_of(row)

    def _source_of(self, row: DictRow) -> Source:
        return self._parse(Source, dict(row))

    async def _version(
        self, cur: Cursor, source_id: UUID, version: int
    ) -> SourceVersion:
        await cur.execute(
            self._sql(
                """
                select {sv_source_id}, {sv_version}, {sv_taken_at}, {sv_taken_by},
                       {sv_connection_id}, {sv_sync_id}, {sv_objects_total},
                       {sv_server_version}
                from {source_versions}
                where {sv_source_id} = %(source_id)s and {sv_version} = %(version)s
                """
            ),
            {"source_id": source_id, "version": version},
        )
        row = await cur.fetchone()
        if row is None:
            raise SourceVersionNotFoundError(source_id, version)

        return self._parse(SourceVersion, dict(row))

    @staticmethod
    def _require_kind(source: Source, snapshot: SourceSnapshot) -> None:
        if snapshot.kind is source.kind:
            return

        msg = (
            f"catalog sources: source {source.id} is {source.kind.value}, "
            f"snapshot is {snapshot.kind.value}"
        )
        raise CatalogStoreError(msg)

    @staticmethod
    def _empty(kind: SourceKind) -> SourceSnapshot:
        if kind is SourceKind.POSTGRES:
            return PgSnapshot.empty()

        return ChSnapshot.empty()

    async def _base_snapshot(
        self, cur: Cursor, source: Source, version: int
    ) -> SourceSnapshot:
        if version == 0:
            return self._empty(source.kind)

        return await self._read_snapshot(cur, source, version)

    # --- внутреннее: строки снимка ---

    async def _insert_snapshot(
        self, cur: Cursor, source_id: UUID, version: int, snapshot: SourceSnapshot
    ) -> None:
        for spec in SnapshotTables.of_kind(snapshot.kind):
            rows: list[dict[str, Any]] = []
            for record in self._records(snapshot, spec):
                rows.append(self._row_of(spec, source_id, version, record))

            if not rows:
                continue

            await cur.executemany(self._insert(spec), rows)

    @staticmethod
    def _records(
        snapshot: SourceSnapshot, spec: SnapshotTable
    ) -> Sequence[SourceRecord]:
        field = SnapshotFields.of_table(spec.table)
        records: object = getattr(snapshot, field)
        if not isinstance(records, tuple):
            msg = f"catalog sources: snapshot field {field} is not a tuple"
            raise CatalogStoreError(msg)

        return records

    @staticmethod
    def _row_of(
        spec: SnapshotTable, source_id: UUID, version: int, record: SourceRecord
    ) -> dict[str, Any]:
        dumped: dict[str, Any] = record.model_dump(mode="json")
        row: dict[str, Any] = {
            SnapshotKey.SOURCE_ID.value: source_id,
            SnapshotKey.VERSION.value: version,
        }
        for column in spec.columns:
            value = dumped[column.field]
            if column.sql_type.is_json and value is not None:
                value = Jsonb(value)

            row[column.column] = value

        return row

    def _insert(self, spec: SnapshotTable) -> sql.Composed:
        idents: list[sql.Composable] = [
            sql.Identifier(SnapshotKey.SOURCE_ID.value),
            sql.Identifier(SnapshotKey.VERSION.value),
        ]
        placeholders: list[sql.Composable] = [
            sql.Placeholder(SnapshotKey.SOURCE_ID.value),
            sql.Placeholder(SnapshotKey.VERSION.value),
        ]
        for column in spec.columns:
            idents.append(sql.Identifier(column.column))
            placeholders.append(sql.Placeholder(column.column))

        return sql.SQL("insert into {} ({}) values ({})").format(
            self._table(spec.table),
            sql.SQL(", ").join(idents),
            sql.SQL(", ").join(placeholders),
        )

    async def _read_snapshot(
        self, cur: Cursor, source: Source, version: int
    ) -> SourceSnapshot:
        fields: dict[str, tuple[SourceRecord, ...]] = {}
        for spec in SnapshotTables.of_kind(source.kind):
            await cur.execute(
                sql.SQL(
                    "select * from {} where {} = %(source_id)s and {} = %(version)s"
                ).format(
                    self._table(spec.table),
                    sql.Identifier(SnapshotKey.SOURCE_ID.value),
                    sql.Identifier(SnapshotKey.VERSION.value),
                ),
                {"source_id": source.id, "version": version},
            )
            rows = await cur.fetchall()
            records: list[SourceRecord] = []
            for row in rows:
                records.append(self._record_of(spec, row))

            fields[SnapshotFields.of_table(spec.table)] = tuple(records)

        try:
            if source.kind is SourceKind.POSTGRES:
                return PgSnapshot.model_validate(fields)

            return ChSnapshot.model_validate(fields)
        except ValidationError as exc:
            msg = (
                f"catalog sources: snapshot rows of {source.id} v{version}"
                " are inconsistent"
            )
            raise CatalogStoreError(msg) from exc

    def _record_of(self, spec: SnapshotTable, row: DictRow) -> SourceRecord:
        payload: dict[str, Any] = {}
        for column in spec.columns:
            payload[column.field] = row[column.column]

        return self._parse(spec.model, payload)

    # --- внутреннее: черновики ---

    async def _draft(self, cur: Cursor, draft_id: UUID, *, lock: bool) -> SourceDraft:
        query: LiteralString = """
            select {sd_id}, {sd_source_id}, {sd_name}, {sd_base_version},
                   {sd_status}, {sd_created_by}, {sd_created_at}
            from {source_drafts}
            where {sd_id} = %(id)s
            """
        if lock:
            query = query + " for update"

        await cur.execute(self._sql(query), {"id": draft_id})
        row = await cur.fetchone()
        if row is None:
            raise SourceDraftNotFoundError(draft_id)

        return self._parse(SourceDraft, dict(row))

    @staticmethod
    def _require_open(draft: SourceDraft) -> None:
        if draft.status is DraftStatus.OPEN:
            return

        raise DraftClosedError(draft.id, draft.status)

    async def _set_status(
        self, cur: Cursor, draft_id: UUID, status: DraftStatus
    ) -> SourceDraft:
        await cur.execute(
            self._sql(
                """
                update {source_drafts}
                set {sd_status} = %(status)s, {sd_closed_at} = now()
                where {sd_id} = %(id)s
                """
            ),
            {"id": draft_id, "status": status.value},
        )
        return await self._draft(cur, draft_id, lock=False)

    async def _ops_of(self, cur: Cursor, draft_id: UUID) -> Sequence[SourceDraftOp]:
        await cur.execute(
            self._sql(
                """
                select {so_draft_id}, {so_seq}, {so_author_id}, {so_via},
                       {so_operations}, {so_created_at}
                from {source_draft_ops}
                where {so_draft_id} = %(draft_id)s
                order by {so_seq}
                """
            ),
            {"draft_id": draft_id},
        )
        rows = await cur.fetchall()
        ops: list[SourceDraftOp] = []
        for row in rows:
            ops.append(
                self._parse(
                    SourceDraftOp,
                    {
                        "draft_id": row["draft_id"],
                        "seq": row["seq"],
                        "author": {"user_id": row["author_id"], "via": row["via"]},
                        "operations": row["operations"],
                        "created_at": row["created_at"],
                    },
                )
            )

        return ops

    @staticmethod
    def _fold(base: SourceSnapshot, ops: Sequence[SourceDraftOp]) -> SourceSnapshot:
        current = base
        for op in ops:
            current = op.operations.apply(current)

        return current

    @staticmethod
    def _parse(model: type[ModelT], payload: dict[str, Any]) -> ModelT:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            msg = f"catalog sources: row of {model.__name__} is malformed"
            raise CatalogStoreError(msg) from exc


class SnapshotFields:
    """Имя поля снимка по таблице хранения."""

    FIELDS: ClassVar[dict[SourceTable, str]] = {
        SourceTable.PG_DATABASES: "databases",
        SourceTable.PG_SCHEMAS: "schemas",
        SourceTable.PG_RELATIONS: "relations",
        SourceTable.PG_COLUMNS: "columns",
        SourceTable.PG_CONSTRAINTS: "constraints",
        SourceTable.PG_INDEXES: "indexes",
        SourceTable.PG_ROUTINES: "routines",
        SourceTable.PG_ROUTINE_ARGS: "routine_args",
        SourceTable.PG_SEQUENCES: "sequences",
        SourceTable.PG_TYPES: "types",
        SourceTable.CH_DATABASES: "databases",
        SourceTable.CH_TABLES: "tables",
        SourceTable.CH_COLUMNS: "columns",
        SourceTable.CH_DICTIONARIES: "dictionaries",
        SourceTable.CH_DICTIONARY_ATTRIBUTES: "dictionary_attributes",
    }

    @classmethod
    def of_table(cls, table: SourceTable) -> str:
        return cls.FIELDS[table]
