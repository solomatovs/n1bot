"""Хранилище снимков подключений в Postgres: версии снимков в родной
структуре (по таблице на род записи, полная копия на версию) и записи
синхронизаций со staging-таблицей порций на время синхронизации. Строки
самих подключений живут у брокера; здесь подключение — только id, а имя и
вид копируются в версию и синхронизацию на момент снятия.

Таблицы снимков выводятся из объявления частей снимка каждого вида
(SourceSnapshot.parts): спецификация SnapshotTable строится по модели записи
и даёт DDL, вставку строк версии и чтение версии обратно в модели домена;
про конкретные виды хранилище ничего не знает. Запись версии — одна
транзакция: номер версии, шапка, все строки.

Ошибки:
CatalogStoreError — Postgres недоступен, ответ битый, строки не складываются
    в снимок.
ConnectionNotSyncedError — у подключения нет версий снимка.
ConnectionVersionNotFoundError — у подключения нет такой версии.
SnapshotKindMismatchError — снимок другого вида, чем прежние версии.
SyncNotFoundError — синхронизации с таким id нет.
SyncRunningError — у подключения уже идёт синхронизация.
SyncClosedError — синхронизация уже завершена.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterator, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from types import NoneType, UnionType
from typing import Any, ClassVar, LiteralString, TypeVar, Union, get_args, get_origin
from uuid import UUID

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
    ConnectionNotSyncedError,
    ConnectionVersion,
    ConnectionVersionNotFoundError,
    SnapshotKindMismatchError,
    StagedBatch,
    Sync,
    SyncClosedError,
    SyncedConnection,
    SyncNotFoundError,
    SyncRequest,
    SyncRunningError,
    SyncStatus,
    VersionOrigin,
)
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresTable, SqlNames

logger = logging.getLogger(__name__)

__all__ = ["ConnectionStore", "ConnectionTable", "StagingTable"]

Cursor = psycopg.AsyncCursor[DictRow]
ModelT = TypeVar("ModelT", bound=BaseModel)


class ConnectionTable(StrEnum):
    """Таблицы снимков подключений в схеме каталога."""

    VERSIONS = "connection_versions"
    SYNCS = "connection_syncs"


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
    """Таблицы снимков всех видов реестра: по части на таблицу, в порядке
    объявления частей (от родителей к детям)."""

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


class VersionsColumn(StrEnum):
    CONNECTION_ID = "connection_id"
    VERSION = "version"
    CONNECTION_NAME = "connection_name"
    KIND = "kind"
    TAKEN_AT = "taken_at"
    TAKEN_BY = "taken_by"
    SYNC_ID = "sync_id"
    OBJECTS_TOTAL = "objects_total"
    SERVER_VERSION = "server_version"


class SyncsColumn(StrEnum):
    ID = "id"
    CONNECTION_ID = "connection_id"
    CONNECTION_NAME = "connection_name"
    KIND = "kind"
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

    CONNECTION_ID = "connection_id"
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


class ConnectionStore(PostgresTable):
    """Хранилище снимков подключений: живёт под CatalogService рядом с
    ProcessStore в той же схеме; прав не знает. Подключение блокируется на
    время записи версии и старта синхронизации advisory-замком по его id."""

    LOCK_PREFIX: ClassVar[str] = "catalog.connection"

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
        cv_ connection_versions, sy_ connection_syncs."""
        names: dict[str, sql.Composable] = {}
        for table in ConnectionTable:
            names[table.value] = self._table(table)

        prefixed: dict[str, type[StrEnum]] = {
            "cv": VersionsColumn,
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
            msg = (
                f"catalog connections: {action} in schema {self._schema} failed: {exc}"
            )
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
        """Схема и таблицы; повтор безвреден. Таблица другого выпуска, которую
        DDL оставил как есть, — отказ с расхождением колонок."""
        async with self._guarded("setup"):
            await self._apply_ddl(self._ddl())
            await self._check_layouts(self._layouts())

        logger.info("catalog connections ready: %s", self._cfg.db_schema)

    def _layouts(self) -> dict[str, list[str]]:
        layouts: dict[str, list[str]] = {
            ConnectionTable.SYNCS.value: list(SyncsColumn),
            ConnectionTable.VERSIONS.value: list(VersionsColumn),
        }
        for spec in self._tables.all():
            names: list[str] = list(SnapshotKey)
            for column in spec.columns:
                names.append(column.column)

            layouts[spec.table] = names

        return layouts

    def _ddl(self) -> tuple[sql.Composed, ...]:
        statements: list[sql.Composed] = [
            self._sql(
                """
                create table if not exists {connection_syncs} (
                    {sy_id}              uuid primary key,
                    {sy_connection_id}   uuid not null,
                    {sy_connection_name} text not null,
                    {sy_kind}            text not null,
                    {sy_started_by}      uuid not null,
                    {sy_started_at}      timestamptz not null default now(),
                    {sy_finished_at}     timestamptz null,
                    {sy_status}          text not null,
                    {sy_scope}           jsonb not null default '{{}}'::jsonb,
                    {sy_objects_total}   integer null,
                    {sy_objects_done}    integer not null default 0,
                    {sy_error}           text null,
                    {sy_version}         integer null
                )
                """
            ),
            self._sql(
                """
                create table if not exists {connection_versions} (
                    {cv_connection_id}   uuid not null,
                    {cv_version}         integer not null,
                    {cv_connection_name} text not null,
                    {cv_kind}            text not null,
                    {cv_taken_at}        timestamptz not null default now(),
                    {cv_taken_by}        uuid not null,
                    {cv_sync_id}         uuid null
                                         references {connection_syncs} ({sy_id}),
                    {cv_objects_total}   integer not null default 0,
                    {cv_server_version}  text null,
                    primary key ({cv_connection_id}, {cv_version})
                )
                """
            ),
        ]
        for spec in self._tables.all():
            statements.append(self._snapshot_ddl(spec))

        return tuple(statements)

    def _snapshot_table(self, spec: SnapshotTable) -> sql.Identifier:
        return sql.Identifier(self._schema, spec.table)

    def _snapshot_ddl(self, spec: SnapshotTable) -> sql.Composed:
        definitions: list[sql.Composable] = [
            sql.SQL("{} uuid not null").format(
                sql.Identifier(SnapshotKey.CONNECTION_ID.value)
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
            sql.Identifier(SnapshotKey.CONNECTION_ID.value),
            sql.Identifier(SnapshotKey.VERSION.value),
        ]
        for name in spec.key:
            key.append(sql.Identifier(name))

        definitions.append(sql.SQL("primary key ({})").format(sql.SQL(", ").join(key)))
        # строки версии уходят вместе с её шапкой: forget_versions удаляет шапки
        definitions.append(
            sql.SQL(
                "foreign key ({}, {}) references {} ({}, {}) on delete cascade"
            ).format(
                sql.Identifier(SnapshotKey.CONNECTION_ID.value),
                sql.Identifier(SnapshotKey.VERSION.value),
                self._table(ConnectionTable.VERSIONS),
                sql.Identifier(VersionsColumn.CONNECTION_ID.value),
                sql.Identifier(VersionsColumn.VERSION.value),
            )
        )

        return sql.SQL("create table if not exists {} ({})").format(
            self._snapshot_table(spec), sql.SQL(", ").join(definitions)
        )

    # --- подключения глазами каталога ---

    async def synced_connections(self) -> Sequence[SyncedConnection]:
        """Подключения с версиями снимка по последней версии каждого."""
        async with self._transaction("list synced connections") as cur:
            await cur.execute(
                self._synced_select(
                    " order by v.{cv_connection_name}, v.{cv_connection_id}"
                )
            )
            rows = await cur.fetchall()

        synced: list[SyncedConnection] = []
        for row in rows:
            synced.append(self._parse(SyncedConnection, dict(row)))

        return synced

    async def synced(self, connection_id: UUID) -> SyncedConnection:
        async with self._transaction(f"synced connection {connection_id}") as cur:
            return await self._synced(cur, connection_id)

    async def synced_or_none(self, connection_id: UUID) -> SyncedConnection | None:
        async with self._transaction(f"synced connection {connection_id}") as cur:
            return await self._synced_or_none(cur, connection_id)

    # --- версии ---

    async def write_version(
        self, connection_id: UUID, snapshot: SourceSnapshot, origin: VersionOrigin
    ) -> ConnectionVersion:
        """Новая версия подключения целиком одной транзакцией. Этим же путём
        синхронизация переносит staging в хранилище."""
        snapshot.check()
        async with self._transaction(
            f"write version of connection {connection_id}"
        ) as cur:
            await self._lock(cur, connection_id)
            return await self._write_version(cur, connection_id, snapshot, origin)

    async def _write_version(
        self,
        cur: Cursor,
        connection_id: UUID,
        snapshot: SourceSnapshot,
        origin: VersionOrigin,
    ) -> ConnectionVersion:
        """Шапка и строки новой версии в открытой транзакции; подключение уже
        под замком.

        Ошибки:
        SnapshotKindMismatchError — прежние версии другого вида.
        """
        current = await self._synced_or_none(cur, connection_id)
        version = 1
        if current is not None:
            if current.kind != snapshot.kind:
                raise SnapshotKindMismatchError(
                    connection_id, current.kind, snapshot.kind
                )

            version = current.latest_version + 1

        await cur.execute(
            self._sql(
                """
                insert into {connection_versions}
                    ({cv_connection_id}, {cv_version}, {cv_connection_name}, {cv_kind},
                     {cv_taken_by}, {cv_sync_id}, {cv_objects_total},
                     {cv_server_version})
                values
                    (%(connection_id)s, %(version)s, %(connection_name)s, %(kind)s,
                     %(taken_by)s, %(sync_id)s, %(objects_total)s, %(server_version)s)
                """
            ),
            {
                "connection_id": connection_id,
                "version": version,
                "connection_name": origin.connection_name,
                "kind": snapshot.kind,
                "taken_by": origin.taken_by,
                "sync_id": origin.sync_id,
                "objects_total": snapshot.objects_count(),
                "server_version": origin.server_version,
            },
        )
        await self._insert_snapshot(cur, connection_id, version, snapshot)
        return await self._version(cur, connection_id, version)

    async def versions_of(self, connection_id: UUID) -> Sequence[ConnectionVersion]:
        async with self._transaction(f"versions of connection {connection_id}") as cur:
            await cur.execute(
                self._version_select(
                    " where {cv_connection_id} = %(connection_id)s"
                    " order by {cv_version}"
                ),
                {"connection_id": connection_id},
            )
            rows = await cur.fetchall()

        versions: list[ConnectionVersion] = []
        for row in rows:
            versions.append(self._parse(ConnectionVersion, dict(row)))

        return versions

    async def version_of(self, connection_id: UUID, version: int) -> ConnectionVersion:
        async with self._transaction(
            f"version {version} of connection {connection_id}"
        ) as cur:
            return await self._version(cur, connection_id, version)

    async def snapshot_of(self, connection_id: UUID, version: int) -> SourceSnapshot:
        """Снимок версии; версия 0 — пустой снимок вида подключения.

        Ошибки:
        ConnectionNotSyncedError — версий нет, вид неизвестен.
        ConnectionVersionNotFoundError — такой версии нет.
        """
        async with self._transaction(
            f"snapshot {version} of connection {connection_id}"
        ) as cur:
            synced = await self._synced(cur, connection_id)
            if version == 0:
                return self._empty(synced.kind)

            await self._version(cur, connection_id, version)
            return await self._read_snapshot(cur, connection_id, synced.kind, version)

    async def latest_snapshot(self, connection_id: UUID) -> SourceSnapshot:
        async with self._transaction(
            f"latest snapshot of connection {connection_id}"
        ) as cur:
            synced = await self._synced(cur, connection_id)
            return await self._read_snapshot(
                cur, connection_id, synced.kind, synced.latest_version
            )

    async def diff_of(self, connection_id: UUID, old: int, new: int) -> SourceDiff:
        before = await self.snapshot_of(connection_id, old)
        after = await self.snapshot_of(connection_id, new)
        return SourceDiff.between(connection_id, before, after)

    async def forget_versions(self, connection_id: UUID) -> int:
        """Все версии снимка подключения со строками; сколько версий было.
        Проверка узлов процессов — на сервисе, здесь только строки.

        Ошибки:
        SyncRunningError — синхронизация ещё пишет.
        """
        async with self._transaction(
            f"forget versions of connection {connection_id}"
        ) as cur:
            await self._lock(cur, connection_id)
            running = await self._running_sync(cur, connection_id)
            if running is not None:
                raise SyncRunningError(connection_id, running.id)

            await cur.execute(
                self._sql(
                    """
                    delete from {connection_versions}
                    where {cv_connection_id} = %(connection_id)s
                    """
                ),
                {"connection_id": connection_id},
            )
            return cur.rowcount

    # --- синхронизации ---

    async def start_sync(
        self, sync_id: UUID, request: SyncRequest, started_by: UUID
    ) -> Sync:
        """Запись синхронизации и её staging-таблица; staging прежних, уже
        закрытых синхронизаций подключения убирается здесь же.

        Ошибки:
        SnapshotKindMismatchError — прежние версии другого вида.
        SyncRunningError — у подключения уже идёт синхронизация.
        """
        connection_id = request.connection_id
        action = f"start sync of connection {connection_id}"
        async with self._transaction(action) as cur:
            await self._lock(cur, connection_id)
            current = await self._synced_or_none(cur, connection_id)
            if current is not None and current.kind != request.kind:
                raise SnapshotKindMismatchError(
                    connection_id, current.kind, request.kind
                )

            running = await self._running_sync(cur, connection_id)
            if running is not None:
                raise SyncRunningError(connection_id, running.id)

            await self._sweep_staging(cur, connection_id)
            await cur.execute(
                self._sql(
                    """
                    insert into {connection_syncs}
                        ({sy_id}, {sy_connection_id}, {sy_connection_name}, {sy_kind},
                         {sy_started_by}, {sy_status}, {sy_scope})
                    values
                        (%(id)s, %(connection_id)s, %(connection_name)s, %(kind)s,
                         %(started_by)s, %(status)s, %(scope)s)
                    """
                ),
                {
                    "id": sync_id,
                    "connection_id": connection_id,
                    "connection_name": request.connection_name,
                    "kind": request.kind,
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
                    update {connection_syncs}
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
                    update {connection_syncs}
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

        snapshot_class = self._kinds.snapshot_class(sync.kind)
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
    ) -> ConnectionVersion:
        """Собранный снимок становится версией подключения, синхронизация
        закрывается итогом и staging убирается — одной транзакцией."""
        snapshot.check()
        async with self._transaction(f"commit sync {sync_id}") as cur:
            sync = await self._sync(cur, sync_id, lock=True)
            self._require_running(sync)
            await self._lock(cur, sync.connection_id)
            origin = VersionOrigin(
                taken_by=sync.started_by,
                connection_name=sync.connection_name,
                sync_id=sync.id,
                server_version=server_version,
            )
            version = await self._write_version(
                cur, sync.connection_id, snapshot, origin
            )
            await cur.execute(
                self._sql(
                    """
                    update {connection_syncs}
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
                    update {connection_syncs}
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

    async def syncs_of(self, connection_id: UUID) -> Sequence[Sync]:
        """Синхронизации подключения, новые первыми."""
        async with self._transaction(f"syncs of connection {connection_id}") as cur:
            tail: LiteralString = (
                " where {sy_connection_id} = %(connection_id)s"
                " order by {sy_started_at} desc"
            )
            await cur.execute(self._sync_select(tail), {"connection_id": connection_id})
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
        select {sy_id}, {sy_connection_id}, {sy_connection_name}, {sy_kind},
               {sy_started_by}, {sy_started_at}, {sy_finished_at}, {sy_status},
               {sy_scope}, {sy_objects_total}, {sy_objects_done}, {sy_error},
               {sy_version}
        from {connection_syncs}
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

    async def _running_sync(self, cur: Cursor, connection_id: UUID) -> Sync | None:
        await cur.execute(
            self._sync_select(
                " where {sy_connection_id} = %(connection_id)s"
                " and {sy_status} = %(status)s"
            ),
            {"connection_id": connection_id, "status": SyncStatus.RUNNING.value},
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

    def _staging_table(self, sync_id: UUID) -> sql.Identifier:
        return sql.Identifier(self._schema, StagingTable.name_of(sync_id))

    async def _drop_staging(self, cur: Cursor, sync_id: UUID) -> None:
        await cur.execute(
            sql.SQL("drop table if exists {}").format(self._staging_table(sync_id))
        )

    async def _sweep_staging(self, cur: Cursor, connection_id: UUID) -> None:
        """Staging закрытых синхронизаций подключения: остаётся после падения
        процесса посреди синхронизации."""
        await cur.execute(
            self._sql(
                """
                select {sy_id} from {connection_syncs}
                where {sy_connection_id} = %(connection_id)s
                  and {sy_status} <> %(status)s
                """
            ),
            {"connection_id": connection_id, "status": SyncStatus.RUNNING.value},
        )
        rows = await cur.fetchall()
        for row in rows:
            await self._drop_staging(cur, row[SyncsColumn.ID.value])

    # --- внутреннее: подключения и версии ---

    async def _lock(self, cur: Cursor, connection_id: UUID) -> None:
        await cur.execute(
            "select pg_advisory_xact_lock(hashtext(%(key)s))",
            {"key": f"{self._schema}.{self.LOCK_PREFIX}.{connection_id}"},
        )

    SYNCED_SELECT: ClassVar[LiteralString] = """
        with
            latest as (
                select
                    w.{cv_connection_id} as connection_id,
                    max(w.{cv_version}) as version
                from
                    {connection_versions} w
                group by
                    w.{cv_connection_id}
            )
        select
            v.{cv_connection_id} as connection_id,
            v.{cv_connection_name} as name,
            v.{cv_kind} as kind,
            v.{cv_version} as latest_version,
            v.{cv_taken_at} as synced_at
        from
            {connection_versions} v
            join latest l
                on l.connection_id = v.{cv_connection_id}
                and l.version = v.{cv_version}
        where 1=1
        """

    def _synced_select(self, tail: LiteralString) -> sql.Composed:
        return sql.Composed([self._sql(self.SYNCED_SELECT), self._sql(tail)])

    async def _synced_or_none(
        self, cur: Cursor, connection_id: UUID
    ) -> SyncedConnection | None:
        await cur.execute(
            self._synced_select(" and v.{cv_connection_id} = %(connection_id)s"),
            {"connection_id": connection_id},
        )
        row = await cur.fetchone()
        if row is None:
            return None

        return self._parse(SyncedConnection, dict(row))

    async def _synced(self, cur: Cursor, connection_id: UUID) -> SyncedConnection:
        synced = await self._synced_or_none(cur, connection_id)
        if synced is None:
            raise ConnectionNotSyncedError(connection_id)

        return synced

    VERSION_SELECT: ClassVar[LiteralString] = """
        select {cv_connection_id}, {cv_version}, {cv_connection_name}, {cv_kind},
               {cv_taken_at}, {cv_taken_by}, {cv_sync_id}, {cv_objects_total},
               {cv_server_version}
        from {connection_versions}
        """

    def _version_select(self, tail: LiteralString) -> sql.Composed:
        return sql.Composed([self._sql(self.VERSION_SELECT), self._sql(tail)])

    async def _version(
        self, cur: Cursor, connection_id: UUID, version: int
    ) -> ConnectionVersion:
        await cur.execute(
            self._version_select(
                " where {cv_connection_id} = %(connection_id)s"
                " and {cv_version} = %(version)s"
            ),
            {"connection_id": connection_id, "version": version},
        )
        row = await cur.fetchone()
        if row is None:
            raise ConnectionVersionNotFoundError(connection_id, version)

        return self._parse(ConnectionVersion, dict(row))

    def _empty(self, kind: str) -> SourceSnapshot:
        return self._kinds.empty(kind)

    # --- внутреннее: строки снимка ---

    async def _insert_snapshot(
        self, cur: Cursor, connection_id: UUID, version: int, snapshot: SourceSnapshot
    ) -> None:
        for spec in self._tables.of_kind(snapshot.kind):
            rows: list[dict[str, Any]] = []
            for record in snapshot.records_of(spec.part.name):
                rows.append(self._row_of(spec, connection_id, version, record))

            if not rows:
                continue

            await cur.executemany(self._insert(spec), rows)

    @staticmethod
    def _row_of(
        spec: SnapshotTable, connection_id: UUID, version: int, record: SourceRecord
    ) -> dict[str, Any]:
        dumped: dict[str, Any] = record.model_dump(mode="json")
        row: dict[str, Any] = {
            SnapshotKey.CONNECTION_ID.value: connection_id,
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
            sql.Identifier(SnapshotKey.CONNECTION_ID.value),
            sql.Identifier(SnapshotKey.VERSION.value),
        ]
        placeholders: list[sql.Composable] = [
            sql.Placeholder(SnapshotKey.CONNECTION_ID.value),
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
        self, cur: Cursor, connection_id: UUID, kind: str, version: int
    ) -> SourceSnapshot:
        fields: dict[str, tuple[SourceRecord, ...]] = {}
        for spec in self._tables.of_kind(kind):
            await cur.execute(
                self._select(spec), {"connection_id": connection_id, "version": version}
            )
            rows = await cur.fetchall()
            records: list[SourceRecord] = []
            for row in rows:
                records.append(self._record_of(spec, row))

            fields[spec.part.name] = tuple(records)

        try:
            return self._kinds.snapshot_class(kind).model_validate(fields)
        except ValidationError as exc:
            msg = (
                f"catalog connections: rows of connection {connection_id} version "
                f"{version} in {self._schema} do not form a valid {kind} "
                f"snapshot: {exc}"
            )
            raise CatalogStoreError(msg) from exc

    def _select(self, spec: SnapshotTable) -> sql.Composed:
        """Колонки части по её спецификации за одну версию подключения."""
        idents: list[sql.Composable] = []
        for column in spec.columns:
            idents.append(sql.Identifier(column.column))

        return sql.SQL(
            "select {} from {} where {} = %(connection_id)s and {} = %(version)s"
        ).format(
            sql.SQL(", ").join(idents),
            self._snapshot_table(spec),
            sql.Identifier(SnapshotKey.CONNECTION_ID.value),
            sql.Identifier(SnapshotKey.VERSION.value),
        )

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
                f"catalog connections: row from {self._schema} does not form "
                f"a valid {model.__name__}: {exc}"
            )
            raise CatalogStoreError(msg) from exc
