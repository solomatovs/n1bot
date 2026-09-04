"""Хранилище источников метаданных в Postgres: источники, привязки
подключений, версии со снимками в родной структуре (по таблице на род
записи, полная копия на версию), записи синхронизаций со staging-таблицей
порций на время синхронизации.

Таблицы снимков выводятся из объявления частей снимка каждого вида
(SourceSnapshot.parts): спецификация SnapshotTable строится по модели записи
и даёт DDL, вставку строк версии и чтение версии обратно в модели домена;
про конкретные виды источников хранилище ничего не знает. Запись версии —
одна транзакция: номер версии, шапка, все строки.

Ошибки:
CatalogStoreError — Postgres недоступен, ответ битый, строки не складываются
    в снимок.
SourceNotFoundError — источника с таким id нет.
SourceVersionNotFoundError — у источника нет такой версии.
SyncNotFoundError — синхронизации с таким id нет.
SyncRunningError — у источника уже идёт синхронизация.
SyncClosedError — синхронизация уже завершена.
SyncConnectionNotBoundError — подключение синхронизации не привязано к источнику.
SourceKindMismatchError — подключение другого вида, чем источник.
ConnectionAlreadyBoundError — подключение уже стоит в другом источнике.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterator, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from types import NoneType, UnionType
from typing import Any, ClassVar, LiteralString, TypeVar, Union, get_args, get_origin
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from boba.catalog import (
    SnapshotPart,
    SourceDiff,
    SourceKinds,
    SourceRecord,
    SourceSnapshot,
    SyncBatch,
    SyncPlan,
)
from boba.catalog_service.config import CatalogConfig
from boba.catalog_service.records import (
    CatalogStoreError,
    ConnectionAlreadyBoundError,
    Source,
    SourceConnection,
    SourceKindMismatchError,
    SourceNotFoundError,
    SourceSpec,
    SourceVersion,
    SourceVersionNotFoundError,
    StagedBatch,
    Sync,
    SyncClosedError,
    SyncConnectionNotBoundError,
    SyncNotFoundError,
    SyncRequest,
    SyncRunningError,
    SyncStatus,
    VersionOrigin,
)
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresTable, SqlNames

logger = logging.getLogger(__name__)

__all__ = ["SourceStore", "SourceTable", "StagingTable"]

Cursor = psycopg.AsyncCursor[DictRow]
ModelT = TypeVar("ModelT", bound=BaseModel)


class SourceTable(StrEnum):
    """Таблицы источников в схеме каталога."""

    SOURCES = "sources"
    SOURCE_CONNECTIONS = "source_connections"
    SOURCE_VERSIONS = "source_versions"
    SYNCS = "syncs"


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

    @classmethod
    def of_annotation(cls, annotation: object) -> SqlType:
        """Тип колонки по аннотации поля модели записи: строки и перечисления
        — text, числа — bigint и real, флаги — boolean, кортежи строк —
        text[], всё остальное (словари, вложенные модели) — jsonb; Optional
        даёт nullable-вариант."""
        nullable = False
        inner = annotation
        origin = get_origin(annotation)
        if origin is Union or origin is UnionType:
            members = [arg for arg in get_args(annotation) if arg is not NoneType]
            nullable = len(members) < len(get_args(annotation))
            if len(members) == 1:
                inner = members[0]
                origin = get_origin(inner)

        base = cls._base_type(inner, origin)
        if not nullable:
            return base

        return cls(f"{base.value} null")

    @classmethod
    def _base_type(cls, inner: object, origin: object) -> SqlType:
        if origin is tuple:
            args = get_args(inner)
            if args and args[0] is str:
                return cls.TEXTS

            return cls.JSONB

        if not isinstance(inner, type):
            return cls.JSONB

        for base, sql_type in cls._scalar_types():
            if issubclass(inner, base):
                return sql_type

        return cls.JSONB

    @classmethod
    def _scalar_types(cls) -> tuple[tuple[type, SqlType], ...]:
        # bool раньше int: bool — подкласс int
        return (
            (bool, cls.BOOL),
            (str, cls.TEXT),
            (int, cls.BIGINT),
            (float, cls.REAL),
        )


class SnapshotColumn:
    """Колонка таблицы снимка: имя поля модели, имя колонки, тип."""

    def __init__(self, field: str, sql_type: SqlType, column: str = "") -> None:
        self.field = field
        self.sql_type = sql_type
        self.column = column or field

    @classmethod
    def of_field(cls, model: type[SourceRecord], field: str) -> SnapshotColumn:
        """Колонка по полю модели: тип из аннотации, имя из COLUMN_NAMES."""
        info = model.model_fields[field]
        column = model.COLUMN_NAMES.get(field, field)
        return cls(field, SqlType.of_annotation(info.annotation), column)


class SnapshotTable:
    """Таблица одной части снимка: имя, модель, колонки и родной ключ,
    выведенные из объявления части и полей её модели."""

    def __init__(
        self,
        table: str,
        part: SnapshotPart,
        columns: Sequence[SnapshotColumn],
        key: Sequence[str],
    ) -> None:
        self.table = table
        self.part = part
        self.model = part.model
        self.columns = tuple(columns)
        self.key = tuple(key)

    @classmethod
    def of(cls, prefix: str, part: SnapshotPart) -> SnapshotTable:
        columns: list[SnapshotColumn] = []
        for field in part.model.model_fields:
            columns.append(SnapshotColumn.of_field(part.model, field))

        key: list[str] = []
        for field in part.model.KEY:
            key.append(part.model.COLUMN_NAMES.get(field, field))

        return cls(f"{prefix}_{part.name}", part, columns, key)

    def column_of(self, field: str) -> str:
        for column in self.columns:
            if column.field == field:
                return column.column

        known: list[str] = []
        for column in self.columns:
            known.append(column.field)

        msg = (
            f"snapshot table {self.table} has no field {field!r}, known fields: {known}"
        )
        raise CatalogStoreError(msg)


class SnapshotTables:
    """Таблицы снимков всех видов источников реестра: по части на таблицу,
    в порядке объявления частей (от родителей к детям)."""

    def __init__(self, kinds: SourceKinds) -> None:
        self._kinds = kinds

    def of_kind(self, kind: str) -> tuple[SnapshotTable, ...]:
        snapshot = self._kinds.snapshot_class(kind)
        tables: list[SnapshotTable] = []
        for part in snapshot.parts():
            tables.append(SnapshotTable.of(snapshot.TABLE_PREFIX, part))

        return tuple(tables)

    def all(self) -> Iterator[SnapshotTable]:
        for snapshot in self._kinds.registered():
            for part in snapshot.parts():
                yield SnapshotTable.of(snapshot.TABLE_PREFIX, part)


class SourcesColumn(StrEnum):
    ID = "id"
    KIND = "kind"
    NAME = "name"
    DESCRIPTION = "description"
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


class SnapshotKey(StrEnum):
    """Служебные колонки каждой таблицы снимка."""

    SOURCE_ID = "source_id"
    VERSION = "version"


class StagingColumn(StrEnum):
    """Колонки staging-таблицы синхронизации: порция как пришла."""

    SEQ = "seq"
    PART = "part"
    RECORDS = "records"
    OBJECTS = "objects"


class StagingTable:
    """Staging-таблица одной синхронизации в схеме каталога: живёт от старта
    до переноса порций в версию, имя — префикс и hex id синхронизации."""

    PREFIX: ClassVar[str] = "sync_"

    @classmethod
    def name_of(cls, sync_id: UUID) -> str:
        return f"{cls.PREFIX}{sync_id.hex}"

    @classmethod
    def is_staging(cls, table: str) -> bool:
        return table.startswith(cls.PREFIX)


class SourceStore(PostgresTable):
    """Хранилище источников: живёт под CatalogService рядом с CatalogStore в
    той же схеме; прав не знает."""

    def __init__(
        self,
        cfg: CatalogConfig,
        kinds: SourceKinds,
        pool: AsyncPostgresPool | None = None,
    ) -> None:
        postgres = cfg.connection
        if pool is None:
            postgres = cfg.require_conn()

        super().__init__(postgres, cfg.db_schema, pool)
        self._cfg = cfg
        self._kinds = kinds
        self._tables = SnapshotTables(kinds)

    @property
    def kinds(self) -> SourceKinds:
        return self._kinds

    def _sql(self, text: LiteralString) -> sql.Composed:
        """SQL с именами таблиц по значению enum и колонок с префиксом:
        s_ sources, sc_ source_connections, sv_ source_versions, sy_ syncs."""
        names: dict[str, sql.Composable] = {}
        for table in SourceTable:
            names[table.value] = self._table(table)

        prefixed: dict[str, type[StrEnum]] = {
            "s": SourcesColumn,
            "sc": SourceConnectionsColumn,
            "sv": SourceVersionsColumn,
            "sy": SyncsColumn,
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
            msg = f"catalog sources: {action} in schema {self._schema} failed: {exc}"
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
        ]
        # одно подключение — не больше чем в одном источнике; дубли из старых
        # развёртываний ломают создание индекса, и ошибка называет таблицу
        statements.append(
            self._sql(
                """
                create unique index if not exists source_connections_connection_uq
                on {source_connections} ({sc_connection_id})
                """
            )
        )
        for spec in self._tables.all():
            statements.append(self._snapshot_ddl(spec))

        return tuple(statements)

    def _snapshot_table(self, spec: SnapshotTable) -> sql.Identifier:
        return sql.Identifier(self._schema, spec.table)

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
            self._snapshot_table(spec), sql.SQL(", ").join(definitions)
        )

    # --- источники ---

    async def create_source(
        self, spec: SourceSpec, kind: str, created_by: UUID
    ) -> Source:
        """Источник заданного вида; вид приходит от первого подключения."""
        source_id = uuid4()
        async with self._transaction(f"create source {spec.name!r}") as cur:
            await cur.execute(
                self._sql(
                    """
                    insert into {sources}
                        ({s_id}, {s_kind}, {s_name}, {s_description}, {s_created_by})
                    values
                        (%(id)s, %(kind)s, %(name)s, %(description)s, %(created_by)s)
                    """
                ),
                {
                    "id": source_id,
                    "kind": kind,
                    "name": spec.name,
                    "description": spec.description,
                    "created_by": created_by,
                },
            )
            return await self._source(cur, source_id)

    async def get_source(self, source_id: UUID) -> Source:
        async with self._transaction(f"get source {source_id}") as cur:
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
        async with self._transaction(f"update source {source_id}") as cur:
            await self._source(cur, source_id)
            await cur.execute(
                self._sql(
                    """
                    update {sources}
                    set {s_name} = %(name)s,
                        {s_description} = %(description)s
                    where {s_id} = %(id)s
                    """
                ),
                {
                    "id": source_id,
                    "name": spec.name,
                    "description": spec.description,
                },
            )
            return await self._source(cur, source_id)

    async def delete_source(self, source_id: UUID) -> bool:
        async with self._transaction(f"delete source {source_id}") as cur:
            await cur.execute(
                self._sql("delete from {sources} where {s_id} = %(id)s"),
                {"id": source_id},
            )
            return cur.rowcount > 0

    # --- подключения ---

    async def bind_connection(
        self, source_id: UUID, connection_id: UUID, kind: str, bound_by: UUID
    ) -> SourceConnection:
        """Привязка подключения вида kind; повтор той же привязки безвреден.

        Ошибки:
        SourceKindMismatchError — вид подключения не совпадает с видом источника.
        ConnectionAlreadyBoundError — подключение стоит в другом источнике.
        """
        async with self._transaction(
            f"bind connection {connection_id} to source {source_id}"
        ) as cur:
            source = await self._source(cur, source_id, lock=True)
            if source.kind != kind:
                raise SourceKindMismatchError(source_id, source.kind, kind)

            holder = await self._holder(cur, connection_id)
            if holder is not None and holder != source_id:
                raise ConnectionAlreadyBoundError(connection_id, holder)

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
            msg = (
                f"catalog sources: select from {self._schema}.source_connections "
                f"returned no row right after binding connection {connection_id} "
                f"to source {source_id}"
            )
            raise CatalogStoreError(msg)

        return self._parse(SourceConnection, dict(row))

    async def unbind_connection(self, source_id: UUID, connection_id: UUID) -> bool:
        async with self._transaction(
            f"unbind connection {connection_id} from source {source_id}"
        ) as cur:
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

    async def holder_of(self, connection_id: UUID) -> Source | None:
        """Источник, в котором стоит подключение; None — свободно."""
        async with self._transaction(f"holder of connection {connection_id}") as cur:
            source_id = await self._holder(cur, connection_id)
            if source_id is None:
                return None

            return await self._source(cur, source_id)

    async def _holder(self, cur: Cursor, connection_id: UUID) -> UUID | None:
        await cur.execute(
            self._sql(
                """
                select {sc_source_id} from {source_connections}
                where {sc_connection_id} = %(connection_id)s
                """
            ),
            {"connection_id": connection_id},
        )
        row = await cur.fetchone()
        if row is None:
            return None

        return row[SourceConnectionsColumn.SOURCE_ID.value]

    async def is_bound(self, source_id: UUID, connection_id: UUID) -> bool:
        async with self._transaction(
            f"check binding of connection {connection_id} to source {source_id}"
        ) as cur:
            await self._source(cur, source_id)
            return await self._is_bound(cur, source_id, connection_id)

    async def connections_of(self, source_id: UUID) -> Sequence[SourceConnection]:
        async with self._transaction(f"connections of source {source_id}") as cur:
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
        async with self._transaction(f"write version of source {source_id}") as cur:
            source = await self._source(cur, source_id, lock=True)
            return await self._write_version(cur, source, snapshot, origin)

    async def _write_version(
        self,
        cur: Cursor,
        source: Source,
        snapshot: SourceSnapshot,
        origin: VersionOrigin,
    ) -> SourceVersion:
        """Шапка и строки новой версии в открытой транзакции; источник уже
        под блокировкой."""
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
                "source_id": source.id,
                "version": version,
                "taken_by": origin.taken_by,
                "connection_id": origin.connection_id,
                "sync_id": origin.sync_id,
                "objects_total": snapshot.objects_count(),
                "server_version": origin.server_version,
            },
        )
        await self._insert_snapshot(cur, source.id, version, snapshot)
        return await self._version(cur, source.id, version)

    async def versions_of(self, source_id: UUID) -> Sequence[SourceVersion]:
        async with self._transaction(f"versions of source {source_id}") as cur:
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
        async with self._transaction(f"version {version} of source {source_id}") as cur:
            return await self._version(cur, source_id, version)

    async def snapshot_of(self, source_id: UUID, version: int) -> SourceSnapshot:
        """Снимок версии; версия 0 — пустой снимок вида источника."""
        async with self._transaction(
            f"snapshot {version} of source {source_id}"
        ) as cur:
            source = await self._source(cur, source_id)
            if version == 0:
                return self._empty(source.kind)

            await self._version(cur, source_id, version)
            return await self._read_snapshot(cur, source, version)

    async def latest_snapshot(self, source_id: UUID) -> SourceSnapshot:
        async with self._transaction(f"latest snapshot of source {source_id}") as cur:
            source = await self._source(cur, source_id)
            if source.latest_version == 0:
                return self._empty(source.kind)

            return await self._read_snapshot(cur, source, source.latest_version)

    async def diff_of(self, source_id: UUID, old: int, new: int) -> SourceDiff:
        before = await self.snapshot_of(source_id, old)
        after = await self.snapshot_of(source_id, new)
        return SourceDiff.between(source_id, before, after)

    # --- синхронизации ---

    async def start_sync(
        self, sync_id: UUID, source_id: UUID, request: SyncRequest, started_by: UUID
    ) -> Sync:
        """Запись синхронизации и её staging-таблица; staging прежних,
        уже закрытых синхронизаций источника убирается здесь же.

        Ошибки:
        SyncConnectionNotBoundError — подключение не привязано к источнику.
        SyncRunningError — у источника уже идёт синхронизация.
        """
        async with self._transaction(f"start sync of source {source_id}") as cur:
            source = await self._source(cur, source_id, lock=True)
            if not await self._is_bound(cur, source_id, request.connection_id):
                raise SyncConnectionNotBoundError(source_id, request.connection_id)

            running = await self._running_sync(cur, source_id)
            if running is not None:
                raise SyncRunningError(source_id, running.id)

            await self._sweep_staging(cur, source_id)
            await cur.execute(
                self._sql(
                    """
                    insert into {syncs}
                        ({sy_id}, {sy_source_id}, {sy_connection_id}, {sy_started_by},
                         {sy_status}, {sy_scope})
                    values
                        (%(id)s, %(source_id)s, %(connection_id)s, %(started_by)s,
                         %(status)s, %(scope)s)
                    """
                ),
                {
                    "id": sync_id,
                    "source_id": source.id,
                    "connection_id": request.connection_id,
                    "started_by": started_by,
                    "status": SyncStatus.RUNNING.value,
                    "scope": Jsonb(request.scope.model_dump(mode="json")),
                },
            )
            await cur.execute(
                sql.SQL(
                    """
                    create table {} (
                        {} integer primary key,
                        {} text not null,
                        {} jsonb not null,
                        {} integer not null
                    )
                    """
                ).format(
                    self._staging_table(sync_id),
                    sql.Identifier(StagingColumn.SEQ.value),
                    sql.Identifier(StagingColumn.PART.value),
                    sql.Identifier(StagingColumn.RECORDS.value),
                    sql.Identifier(StagingColumn.OBJECTS.value),
                )
            )
            return await self._sync(cur, sync_id)

    async def plan_sync(self, sync_id: UUID, plan: SyncPlan) -> Sync:
        """План инструмента записан: сколько объектов ожидается."""
        async with self._transaction(f"plan sync {sync_id}") as cur:
            sync = await self._sync(cur, sync_id, lock=True)
            self._require_running(sync)
            await cur.execute(
                self._sql(
                    """
                    update {syncs}
                    set {sy_objects_total} = %(objects_total)s
                    where {sy_id} = %(id)s
                    """
                ),
                {"id": sync_id, "objects_total": plan.objects_total},
            )
            return await self._sync(cur, sync_id)

    async def stage_batch(
        self, sync_id: UUID, batch: SyncBatch, records: Sequence[SourceRecord]
    ) -> Sync:
        """Порция в staging и продвинутый счётчик объектов."""
        dumped: list[dict[str, Any]] = []
        for record in records:
            dumped.append(record.model_dump(mode="json"))

        action = f"stage batch #{batch.seq} of sync {sync_id}"
        async with self._transaction(action) as cur:
            sync = await self._sync(cur, sync_id, lock=True)
            self._require_running(sync)
            insert: LiteralString = (
                "insert into {} ({}, {}, {}, {}) values (%s, %s, %s, %s)"
            )
            await cur.execute(
                sql.SQL(insert).format(
                    self._staging_table(sync_id),
                    sql.Identifier(StagingColumn.SEQ.value),
                    sql.Identifier(StagingColumn.PART.value),
                    sql.Identifier(StagingColumn.RECORDS.value),
                    sql.Identifier(StagingColumn.OBJECTS.value),
                ),
                (batch.seq, batch.part, Jsonb(dumped), batch.objects),
            )
            await cur.execute(
                self._sql(
                    """
                    update {syncs}
                    set {sy_objects_done} = {sy_objects_done} + %(objects)s
                    where {sy_id} = %(id)s
                    """
                ),
                {"id": sync_id, "objects": batch.objects},
            )
            return await self._sync(cur, sync_id)

    async def staged_batches(self, sync_id: UUID) -> Sequence[StagedBatch]:
        """Порции staging по порядку с разобранными записями своей части."""
        async with self._transaction(f"staged batches of sync {sync_id}") as cur:
            sync = await self._sync(cur, sync_id)
            source = await self._source(cur, sync.source_id)
            await cur.execute(
                sql.SQL("select {}, {}, {}, {} from {} order by {}").format(
                    sql.Identifier(StagingColumn.SEQ.value),
                    sql.Identifier(StagingColumn.PART.value),
                    sql.Identifier(StagingColumn.RECORDS.value),
                    sql.Identifier(StagingColumn.OBJECTS.value),
                    self._staging_table(sync_id),
                    sql.Identifier(StagingColumn.SEQ.value),
                )
            )
            rows = await cur.fetchall()

        snapshot_class = self._kinds.snapshot_class(source.kind)
        batches: list[StagedBatch] = []
        for row in rows:
            part = snapshot_class.part(row[StagingColumn.PART.value])
            records: list[SourceRecord] = []
            for payload in row[StagingColumn.RECORDS.value]:
                records.append(self._parse(part.model, payload))

            batch = SyncBatch(
                seq=row[StagingColumn.SEQ.value],
                part=part.name,
                count=len(records),
                objects=row[StagingColumn.OBJECTS.value],
            )
            batches.append(StagedBatch(batch=batch, records=tuple(records)))

        return batches

    async def commit_sync(
        self, sync_id: UUID, snapshot: SourceSnapshot, server_version: str
    ) -> SourceVersion:
        """Собранный снимок становится версией источника, синхронизация
        закрывается итогом и staging убирается — одной транзакцией."""
        snapshot.check()
        async with self._transaction(f"commit sync {sync_id}") as cur:
            sync = await self._sync(cur, sync_id, lock=True)
            self._require_running(sync)
            source = await self._source(cur, sync.source_id, lock=True)
            origin = VersionOrigin(
                taken_by=sync.started_by,
                connection_id=sync.connection_id,
                sync_id=sync.id,
                server_version=server_version,
            )
            version = await self._write_version(cur, source, snapshot, origin)
            await cur.execute(
                self._sql(
                    """
                    update {syncs}
                    set {sy_status} = %(status)s,
                        {sy_finished_at} = now(),
                        {sy_objects_done} = %(objects_done)s,
                        {sy_version} = %(version)s
                    where {sy_id} = %(id)s
                    """
                ),
                {
                    "id": sync_id,
                    "status": SyncStatus.DONE.value,
                    "objects_done": snapshot.objects_count(),
                    "version": version.version,
                },
            )
            await self._drop_staging(cur, sync_id)
            return version

    async def close_sync(self, sync_id: UUID, status: SyncStatus, error: str) -> Sync:
        """Синхронизация сорвалась или отменена: итог с причиной, staging убран.

        Ошибки:
        SyncClosedError — синхронизация уже закрыта.
        """
        async with self._transaction(f"close sync {sync_id} as {status.value}") as cur:
            sync = await self._sync(cur, sync_id, lock=True)
            self._require_running(sync)
            await cur.execute(
                self._sql(
                    """
                    update {syncs}
                    set {sy_status} = %(status)s,
                        {sy_finished_at} = now(),
                        {sy_error} = %(error)s
                    where {sy_id} = %(id)s
                    """
                ),
                {"id": sync_id, "status": status.value, "error": error},
            )
            await self._drop_staging(cur, sync_id)
            return await self._sync(cur, sync_id)

    async def get_sync(self, sync_id: UUID) -> Sync:
        async with self._transaction(f"get sync {sync_id}") as cur:
            return await self._sync(cur, sync_id)

    async def syncs_of(self, source_id: UUID) -> Sequence[Sync]:
        """Синхронизации источника, новые первыми."""
        async with self._transaction(f"syncs of source {source_id}") as cur:
            await self._source(cur, source_id)
            tail: LiteralString = (
                " where {sy_source_id} = %(source_id)s order by {sy_started_at} desc"
            )
            await cur.execute(self._sync_select(tail), {"source_id": source_id})
            rows = await cur.fetchall()

        return self._syncs_of(rows)

    async def running_syncs(self) -> Sequence[Sync]:
        async with self._transaction("running syncs") as cur:
            tail: LiteralString = (
                " where {sy_status} = %(status)s order by {sy_started_at}"
            )
            params = {"status": SyncStatus.RUNNING.value}
            await cur.execute(self._sync_select(tail), params)
            rows = await cur.fetchall()

        return self._syncs_of(rows)

    # --- внутреннее: синхронизации ---

    SYNC_SELECT: ClassVar[LiteralString] = """
        select {sy_id}, {sy_source_id}, {sy_connection_id}, {sy_started_by},
               {sy_started_at}, {sy_finished_at}, {sy_status}, {sy_scope},
               {sy_objects_total}, {sy_objects_done}, {sy_error}, {sy_version}
        from {syncs}
        """

    def _sync_select(self, tail: LiteralString) -> sql.Composed:
        return sql.Composed([self._sql(self.SYNC_SELECT), self._sql(tail)])

    async def _sync(self, cur: Cursor, sync_id: UUID, *, lock: bool = False) -> Sync:
        tail: LiteralString = " where {sy_id} = %(id)s"
        if lock:
            tail = " where {sy_id} = %(id)s for update"

        await cur.execute(self._sync_select(tail), {"id": sync_id})
        row = await cur.fetchone()
        if row is None:
            raise SyncNotFoundError(sync_id)

        return self._parse(Sync, dict(row))

    def _syncs_of(self, rows: Sequence[DictRow]) -> Sequence[Sync]:
        syncs: list[Sync] = []
        for row in rows:
            syncs.append(self._parse(Sync, dict(row)))

        return syncs

    async def _running_sync(self, cur: Cursor, source_id: UUID) -> Sync | None:
        await cur.execute(
            self._sync_select(
                " where {sy_source_id} = %(source_id)s and {sy_status} = %(status)s"
            ),
            {"source_id": source_id, "status": SyncStatus.RUNNING.value},
        )
        row = await cur.fetchone()
        if row is None:
            return None

        return self._parse(Sync, dict(row))

    @staticmethod
    def _require_running(sync: Sync) -> None:
        if sync.status is SyncStatus.RUNNING:
            return

        raise SyncClosedError(sync.id, sync.status)

    async def _is_bound(
        self, cur: Cursor, source_id: UUID, connection_id: UUID
    ) -> bool:
        await cur.execute(
            self._sql(
                """
                select 1 from {source_connections}
                where {sc_source_id} = %(source_id)s
                  and {sc_connection_id} = %(connection_id)s
                """
            ),
            {"source_id": source_id, "connection_id": connection_id},
        )
        row = await cur.fetchone()
        return row is not None

    def _staging_table(self, sync_id: UUID) -> sql.Identifier:
        return sql.Identifier(self._schema, StagingTable.name_of(sync_id))

    async def _drop_staging(self, cur: Cursor, sync_id: UUID) -> None:
        await cur.execute(
            sql.SQL("drop table if exists {}").format(self._staging_table(sync_id))
        )

    async def _sweep_staging(self, cur: Cursor, source_id: UUID) -> None:
        """Staging закрытых синхронизаций источника: остаётся после падения
        процесса посреди синхронизации."""
        await cur.execute(
            self._sql(
                """
                select {sy_id} from {syncs}
                where {sy_source_id} = %(source_id)s and {sy_status} <> %(status)s
                """
            ),
            {"source_id": source_id, "status": SyncStatus.RUNNING.value},
        )
        rows = await cur.fetchall()
        for row in rows:
            await self._drop_staging(cur, row[SyncsColumn.ID.value])

    # --- внутреннее: источники и версии ---

    SOURCE_SELECT: ClassVar[LiteralString] = """
        select s.{s_id}, s.{s_kind}, s.{s_name}, s.{s_description},
               s.{s_created_by}, s.{s_created_at},
               coalesce((select max(v.{sv_version}) from {source_versions} v
                         where v.{sv_source_id} = s.{s_id}), 0) as latest_version,
               coalesce((select array_agg(c.{sc_connection_id} order by c.{sc_bound_at})
                         from {source_connections} c
                         where c.{sc_source_id} = s.{s_id}), '{{}}') as connection_ids
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
        if snapshot.kind == source.kind:
            return

        msg = (
            f"catalog sources: source {source.id} is {source.kind}, "
            f"snapshot is {snapshot.kind}"
        )
        raise CatalogStoreError(msg)

    def _empty(self, kind: str) -> SourceSnapshot:
        return self._kinds.empty(kind)

    # --- внутреннее: строки снимка ---

    async def _insert_snapshot(
        self, cur: Cursor, source_id: UUID, version: int, snapshot: SourceSnapshot
    ) -> None:
        for spec in self._tables.of_kind(snapshot.kind):
            rows: list[dict[str, Any]] = []
            for record in snapshot.records_of(spec.part.name):
                rows.append(self._row_of(spec, source_id, version, record))

            if not rows:
                continue

            await cur.executemany(self._insert(spec), rows)

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
            self._snapshot_table(spec),
            sql.SQL(", ").join(idents),
            sql.SQL(", ").join(placeholders),
        )

    async def _read_snapshot(
        self, cur: Cursor, source: Source, version: int
    ) -> SourceSnapshot:
        fields: dict[str, tuple[SourceRecord, ...]] = {}
        for spec in self._tables.of_kind(source.kind):
            await cur.execute(
                sql.SQL(
                    "select * from {} where {} = %(source_id)s and {} = %(version)s"
                ).format(
                    self._snapshot_table(spec),
                    sql.Identifier(SnapshotKey.SOURCE_ID.value),
                    sql.Identifier(SnapshotKey.VERSION.value),
                ),
                {"source_id": source.id, "version": version},
            )
            rows = await cur.fetchall()
            records: list[SourceRecord] = []
            for row in rows:
                records.append(self._record_of(spec, row))

            fields[spec.part.name] = tuple(records)

        try:
            return self._kinds.snapshot_class(source.kind).model_validate(fields)
        except ValidationError as exc:
            msg = (
                f"catalog sources: rows of source {source.id} version {version} "
                f"in {self._schema} do not form a valid {source.kind} "
                f"snapshot: {exc}"
            )
            raise CatalogStoreError(msg) from exc

    def _record_of(self, spec: SnapshotTable, row: DictRow) -> SourceRecord:
        payload: dict[str, Any] = {}
        for column in spec.columns:
            payload[column.field] = row[column.column]

        return self._parse(spec.model, payload)

    def _parse(self, model: type[ModelT], payload: dict[str, Any]) -> ModelT:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            msg = (
                f"catalog sources: row from {self._schema} does not form "
                f"a valid {model.__name__}: {exc}"
            )
            raise CatalogStoreError(msg) from exc
