"""Хранилище подключений каталога в Postgres: версии снимков и записи
синхронизаций в схеме приложения, строки снимков и связи — в схеме домена
(таблицы pg_*, ch_*, link по раскладке boba.db.postgres.catalog). Строки
самих подключений живут у брокера; здесь подключение — только id, а имя и
вид копируются в версию и синхронизацию на момент снятия.

Строки версии в домен кладёт инструмент снятия (SnapshotWriter); хранилище
по его итогу записывает шапку версии и закрывает синхронизацию. Путь стенда
и ручной записи — write_version: строки и шапка одной транзакцией.

Ошибки:
CatalogStoreError — Postgres недоступен, ответ битый, строки не складываются
    в снимок.
ConnectionNotSyncedError — у подключения нет версий снимка.
ConnectionVersionNotFoundError — у подключения нет такой версии.
SnapshotKindMismatchError — снимок другого вида, чем прежние версии.
UnknownSourceKindError — вида подключения нет в реестре снимков.
SyncNotFoundError — синхронизации с таким id нет.
SyncRunningError — у подключения уже идёт синхронизация.
SyncClosedError — синхронизация уже завершена.
SyncOutcomeError — инструмент снятия отчитался версией, которой в домене нет.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, LiteralString
from uuid import UUID

from psycopg import sql
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from boba.catalog import SourceDiff, SourceKinds, SourceSnapshot, TreeScope
from boba.catalog_service.config import CatalogConfig
from boba.catalog_service.records import (
    ConnectionNotSyncedError,
    ConnectionVersion,
    ConnectionVersionNotFoundError,
    SnapshotKindMismatchError,
    Sync,
    SyncClosedError,
    SyncedConnection,
    SyncNotFoundError,
    SyncOutcomeError,
    SyncRequest,
    SyncRunningError,
    SyncStatus,
    UnknownSourceKindError,
    VersionOrigin,
)
from boba.catalog_service.store_base import CatalogStoreBase, Cursor
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.catalog import (
    CatalogDomain,
    DomainVersions,
    SnapshotKey,
    SnapshotOutcome,
    SnapshotReader,
    SnapshotTable,
    SnapshotTables,
    StagingTable,
)

logger = logging.getLogger(__name__)

__all__ = ["ConnectionStore", "ConnectionTable"]


class ConnectionTable(StrEnum):
    """Таблицы подключений в схеме приложения."""

    VERSIONS = "connection_versions"
    SYNCS = "connection_syncs"


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


class NewVersion(BaseModel):
    """Шапка версии подключения к записи: номер, вид, число объектов и
    происхождение."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: UUID
    version: int
    kind: str
    objects_total: int
    origin: VersionOrigin


class LegacyStaging:
    """Staging синхронизаций прежних выпусков в схеме приложения: сносится
    при старте."""

    PATTERN: ClassVar[str] = "sync\\_%"


class ConnectionStore(CatalogStoreBase):
    """Хранилище подключений: строки снимков и связи — в схеме домена
    (db_schema), версии и синхронизации — в схеме приложения (app_schema)
    рядом с ProcessStore; прав не знает. Подключение блокируется на время
    записи версии и старта синхронизации advisory-замком по его id."""

    LOCK_PREFIX: ClassVar[str] = "catalog.connection"
    TABLES: ClassVar[type[StrEnum]] = ConnectionTable
    PREFIXED: ClassVar[Mapping[str, type[StrEnum]]] = {
        "cv": VersionsColumn,
        "sy": SyncsColumn,
    }
    LABEL: ClassVar[str] = "catalog connections"

    def __init__(
        self,
        cfg: CatalogConfig,
        kinds: SourceKinds,
        pool: AsyncPostgresPool | None = None,
    ) -> None:
        super().__init__(cfg, cfg.db_schema, pool)
        self._app = cfg.app_schema
        self._kinds = kinds
        self._tables = SnapshotTables(kinds)
        self._domain = CatalogDomain(cfg.db_schema, self._tables)
        self._reader = SnapshotReader(cfg.db_schema, kinds)

    @property
    def kinds(self) -> SourceKinds:
        return self._kinds

    def snapshot_class(self, kind: str) -> type[SourceSnapshot]:
        """Класс снимка вида подключения.

        Ошибки:
        UnknownSourceKindError — снимка этого вида нет в реестре.
        """
        if not self._kinds.known(kind):
            raise UnknownSourceKindError(kind, self._kinds.kinds())

        return self._kinds.snapshot_class(kind)

    def _named_table(self, table: StrEnum) -> sql.Identifier:
        """Таблицы версий и синхронизаций живут в схеме приложения."""
        return sql.Identifier(self._app, table.value)

    async def setup(self) -> None:
        """Схемы и таблицы; повтор безвреден. Таблицы прежних выпусков
        переводятся на месте: таблицы приложения из схемы домена — в схему
        приложения, таблицы снимков — на суррогатный ключ без связи с
        версиями; иная раскладка — отказ с расхождением колонок."""
        async with self._guarded("setup"):
            await self._apply_ddl((), self._app)
            await self._migrate()
            await self._apply_ddl(self._ddl(), self._app)
            await self._check_layouts(self._domain.layouts())
            await self._check_layouts(self._app_layouts(), self._app)

        logger.info(
            "catalog connections ready: %s (domain), %s (app)", self._schema, self._app
        )

    async def _migrate(self) -> None:
        """Перевод прежних выпусков на месте одной транзакцией."""
        async with self._transaction("migrate older releases") as cur:
            for table in ConnectionTable:
                await self._move_to_app(cur, table.value)

            for spec in self._tables.all():
                await self._migrate_snapshot_table(cur, spec)

            await self._drop_legacy_staging(cur)

    async def _move_to_app(self, cur: Cursor, table: str) -> None:
        """Таблица приложения, оставшаяся в схеме домена, переезжает; пустой
        дубль, созданный там прежним выпуском рядом с уже переехавшей, — сносится."""
        await self._move_table(cur, self._schema, self._app, table)

    async def _drop_legacy_staging(self, cur: Cursor) -> None:
        await StagingTable.drop_matching(cur, self._app, LegacyStaging.PATTERN)

    async def _migrate_snapshot_table(self, cur: Cursor, spec: SnapshotTable) -> None:
        """Таблица снимка прежнего выпуска: внешние ключи снимаются, ключ
        строки — bigserial id, прежний ключ версии — unique."""
        if not await self._table_exists(cur, self._schema, spec.table):
            return

        table = self._snapshot_table(spec)
        for name in await self._constraints(cur, spec.table, "f"):
            await cur.execute(
                sql.SQL("alter table {} drop constraint {}").format(
                    table, sql.Identifier(name)
                )
            )

        await cur.execute(
            "select data_type from information_schema.columns "
            "where table_schema = %(schema)s and table_name = %(table)s "
            "and column_name = %(column)s",
            {
                "schema": self._schema,
                "table": spec.table,
                "column": SnapshotKey.ID.value,
            },
        )
        id_column = await cur.fetchone()
        if id_column is not None and id_column["data_type"] == "bigint":
            return

        for name in await self._constraints(cur, spec.table, "p"):
            await cur.execute(
                sql.SQL("alter table {} drop constraint {}").format(
                    table, sql.Identifier(name)
                )
            )

        if id_column is not None:
            await cur.execute(
                sql.SQL("alter table {} drop column {}").format(
                    table, sql.Identifier(SnapshotKey.ID.value)
                )
            )

        await cur.execute(
            sql.SQL("alter table {} add column {} bigserial primary key").format(
                table, sql.Identifier(SnapshotKey.ID.value)
            )
        )
        if not await self._constraints(cur, spec.table, "u"):
            await cur.execute(
                sql.SQL("alter table {} add constraint {} unique ({})").format(
                    table, sql.Identifier(f"{spec.table}_key"), spec.native_key()
                )
            )

    async def _constraints(self, cur: Cursor, table: str, kind: str) -> list[str]:
        """Имена ограничений таблицы домена данного вида (p, u, f)."""
        await cur.execute(
            """
            select c.conname
            from pg_constraint c
                join pg_class r on r.oid = c.conrelid
                join pg_namespace n on n.oid = r.relnamespace
            where c.contype = %(kind)s
              and n.nspname = %(schema)s and r.relname = %(table)s
            """,
            {"kind": kind, "schema": self._schema, "table": table},
        )
        rows = await cur.fetchall()
        names: list[str] = []
        for row in rows:
            names.append(str(row["conname"]))

        return names

    def _app_layouts(self) -> dict[str, list[str]]:
        return {
            ConnectionTable.SYNCS.value: list(SyncsColumn),
            ConnectionTable.VERSIONS.value: list(VersionsColumn),
        }

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
        statements.extend(self._domain.ddl())
        return tuple(statements)

    def _snapshot_table(self, spec: SnapshotTable) -> sql.Identifier:
        return sql.Identifier(self._schema, spec.table)

    # --- подключения глазами каталога ---

    async def synced_connections(self) -> Sequence[SyncedConnection]:
        """Подключения с версиями снимка по последней версии каждого."""
        async with self._transaction("list synced connections") as cur:
            await cur.execute(
                sql.Composed(
                    [
                        sql.SQL("select * from ("),
                        self._latest_select(""),
                        self._sql(
                            ") latest order by {cv_connection_name}, {cv_connection_id}"
                        ),
                    ]
                )
            )
            rows = await cur.fetchall()

        synced: list[SyncedConnection] = []
        for latest in self._parse_all(ConnectionVersion, rows):
            synced.append(SyncedConnection.of(latest))

        return synced

    async def synced(self, connection_id: UUID) -> SyncedConnection:
        async with self._transaction(f"synced connection {connection_id}") as cur:
            return await self._synced(cur, connection_id)

    async def synced_or_none(self, connection_id: UUID) -> SyncedConnection | None:
        async with self._transaction(f"synced connection {connection_id}") as cur:
            return await self._synced_or_none(cur, connection_id)

    # --- версии ---
    # --- версии ---

    async def write_version(
        self, connection_id: UUID, snapshot: SourceSnapshot, origin: VersionOrigin
    ) -> ConnectionVersion:
        """Новая версия подключения целиком одной транзакцией: номер за
        последней версией в домене, строки, шапка. Путь стенда и ручной
        записи; инструмент снятия пишет строки сам."""
        snapshot.check()
        async with self._transaction(
            f"write version of connection {connection_id}"
        ) as cur:
            await self._advisory_lock(cur, self.LOCK_PREFIX, connection_id)
            return await self._write_version(cur, connection_id, snapshot, origin)

    async def _write_version(
        self,
        cur: Cursor,
        connection_id: UUID,
        snapshot: SourceSnapshot,
        origin: VersionOrigin,
    ) -> ConnectionVersion:
        """Ошибки:
        SnapshotKindMismatchError — прежние версии другого вида.
        """
        versions = DomainVersions(self._schema, type(snapshot))
        version = await self._latest_domain_version(cur, versions, connection_id) + 1
        await self._insert_snapshot(cur, connection_id, version, snapshot)
        header = NewVersion(
            connection_id=connection_id,
            version=version,
            kind=snapshot.kind,
            objects_total=snapshot.objects_count(),
            origin=origin,
        )
        await self._version_header(cur, header)
        return await self._version(cur, connection_id, version)

    async def _latest_domain_version(
        self, cur: Cursor, versions: DomainVersions, connection_id: UUID
    ) -> int:
        """Последняя версия подключения по строкам домена; 0 — строк нет."""
        await cur.execute(versions.latest_query(), {"connection_id": connection_id})
        row = await cur.fetchone()
        if row is None:
            return 0

        return int(row["version"])

    async def _version_header(self, cur: Cursor, header: NewVersion) -> None:
        """Шапка версии подключения в открытой транзакции.

        Ошибки:
        SnapshotKindMismatchError — прежние версии другого вида.
        """
        connection_id = header.connection_id
        await self._require_same_kind(cur, connection_id, header.kind)

        origin = header.origin

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
                "version": header.version,
                "connection_name": origin.connection_name,
                "kind": header.kind,
                "taken_by": origin.taken_by,
                "sync_id": origin.sync_id,
                "objects_total": header.objects_total,
                "server_version": origin.server_version,
            },
        )

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
            versions.append(self._parse(ConnectionVersion, row))

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
                return self._kinds.empty(synced.kind)

            await self._version(cur, connection_id, version)
            return await self._reader.read(cur, connection_id, synced.kind, version)

    async def tree_snapshot(
        self, connection_id: UUID, version: int, scope: TreeScope
    ) -> SourceSnapshot:
        """Частичный снимок версии: только записи областей scope — дерево
        отдаёт детей одного пути, не читая снимок целиком. Версия 0 — пустой
        снимок вида.

        Ошибки:
        ConnectionNotSyncedError — версий нет, вид неизвестен.
        ConnectionVersionNotFoundError — такой версии нет.
        """
        async with self._transaction(
            f"tree rows of snapshot {version} of connection {connection_id}"
        ) as cur:
            synced = await self._synced(cur, connection_id)
            if version == 0:
                return self._kinds.empty(synced.kind)

            await self._version(cur, connection_id, version)
            return await self._reader.read_scoped(
                cur, connection_id, synced.kind, version, scope
            )

    async def latest_snapshot(self, connection_id: UUID) -> SourceSnapshot:
        async with self._transaction(
            f"latest snapshot of connection {connection_id}"
        ) as cur:
            synced = await self._synced(cur, connection_id)
            return await self._reader.read(
                cur, connection_id, synced.kind, synced.latest_version
            )

    async def diff_of(self, connection_id: UUID, old: int, new: int) -> SourceDiff:
        before = await self.snapshot_of(connection_id, old)
        after = await self.snapshot_of(connection_id, new)
        return SourceDiff.between(connection_id, before, after)

    async def forget_versions(self, connection_id: UUID) -> int:
        """Все версии снимка подключения со строками и staging; сколько
        версий было. Проверка узлов процессов — на сервисе, здесь только
        строки.

        Ошибки:
        SyncRunningError — синхронизация ещё пишет.
        """
        async with self._transaction(
            f"forget versions of connection {connection_id}"
        ) as cur:
            await self._advisory_lock(cur, self.LOCK_PREFIX, connection_id)
            running = await self._running_sync(cur, connection_id)
            if running is not None:
                raise SyncRunningError(connection_id, running.id)

            synced = await self._synced_or_none(cur, connection_id)
            if synced is not None:
                for spec in self._tables.of_kind(synced.kind):
                    await cur.execute(
                        sql.SQL("delete from {} where {} = %(connection_id)s").format(
                            self._snapshot_table(spec),
                            sql.Identifier(SnapshotKey.CONNECTION_ID.value),
                        ),
                        {"connection_id": connection_id},
                    )

            await self._drop_staging(cur, connection_id)
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

    async def _drop_staging(self, cur: Cursor, connection_id: UUID) -> None:
        """Staging снятия подключения в схеме домена, оставшийся после
        сорвавшегося инструмента."""
        pattern = StagingTable.pattern_of(connection_id)
        await StagingTable.drop_matching(cur, self._schema, pattern)

    # --- синхронизации ---

    async def start_sync(
        self, sync_id: UUID, request: SyncRequest, started_by: UUID
    ) -> Sync:
        """Запись синхронизации; строки в домен положит инструмент снятия.

        Ошибки:
        SnapshotKindMismatchError — прежние версии другого вида.
        SyncRunningError — у подключения уже идёт синхронизация.
        """
        connection_id = request.connection.id
        action = f"start sync of connection {connection_id}"
        async with self._transaction(action) as cur:
            await self._advisory_lock(cur, self.LOCK_PREFIX, connection_id)
            await self._require_same_kind(cur, connection_id, request.connection.kind)

            running = await self._running_sync(cur, connection_id)
            if running is not None:
                raise SyncRunningError(connection_id, running.id)

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
                    "connection_name": request.connection.name,
                    "kind": request.connection.kind,
                    "started_by": started_by,
                    "status": SyncStatus.RUNNING.value,
                    "scope": Jsonb(request.scope.model_dump(mode="json")),
                },
            )
            return await self._sync(cur, sync_id)

    async def record_sync(self, sync_id: UUID, outcome: SnapshotOutcome) -> Sync:
        """Инструмент снятия положил версию в домен: шапка версии по его
        итогу, число объектов — по строкам домена, синхронизация закрыта
        итогом — одной транзакцией.

        Ошибки:
        SyncOutcomeError — версии из итога в домене нет.
        SyncClosedError — синхронизация уже закрыта.
        SnapshotKindMismatchError — прежние версии другого вида.
        """
        async with self._transaction(f"record sync {sync_id}") as cur:
            sync = await self._sync(cur, sync_id, lock=True)
            self._require_running(sync)
            await self._advisory_lock(cur, self.LOCK_PREFIX, sync.connection_id)
            snapshot_class = self._kinds.snapshot_class(sync.kind)
            versions = DomainVersions(self._schema, snapshot_class)
            connection_id = sync.connection_id
            latest = await self._latest_domain_version(cur, versions, connection_id)
            if latest < outcome.version:
                raise SyncOutcomeError(sync_id, outcome.version, latest)

            objects = await self._domain_objects(cur, versions, sync, outcome.version)
            header = NewVersion(
                connection_id=connection_id,
                version=outcome.version,
                kind=sync.kind,
                objects_total=objects,
                origin=VersionOrigin(
                    taken_by=sync.started_by,
                    connection_name=sync.connection_name,
                    sync_id=sync.id,
                    server_version=outcome.server_version,
                ),
            )
            await self._version_header(cur, header)
            await cur.execute(
                self._sql(
                    """
                    update {connection_syncs}
                    set {sy_status} = %(status)s,
                        {sy_finished_at} = now(),
                        {sy_objects_total} = %(objects)s,
                        {sy_objects_done} = %(objects)s,
                        {sy_version} = %(version)s
                    where {sy_id} = %(id)s
                    """
                ),
                {
                    "id": sync_id,
                    "status": SyncStatus.DONE.value,
                    "objects": objects,
                    "version": outcome.version,
                },
            )
            return await self._sync(cur, sync_id)

    async def _domain_objects(
        self, cur: Cursor, versions: DomainVersions, sync: Sync, version: int
    ) -> int:
        await cur.execute(
            versions.objects_query(),
            {"connection_id": sync.connection_id, "version": version},
        )
        row = await cur.fetchone()
        if row is None:
            return 0

        return int(row["objects"])

    async def close_sync(self, sync_id: UUID, status: SyncStatus, error: str) -> Sync:
        """Синхронизация сорвалась или отменена: итог с причиной; staging
        инструмента в домене остаётся до следующего снятия или забвения
        версий.

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

        return self._parse(Sync, row)

    def _syncs_of(self, rows: Sequence[DictRow]) -> Sequence[Sync]:
        syncs: list[Sync] = []
        for row in rows:
            syncs.append(self._parse(Sync, row))

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

        return self._parse(Sync, row)

    @staticmethod
    def _require_running(sync: Sync) -> None:
        if sync.status is SyncStatus.RUNNING:
            return

        raise SyncClosedError(sync.id, sync.status)

    async def _require_same_kind(
        self, cur: Cursor, connection_id: UUID, kind: str
    ) -> None:
        """Ошибки:
        SnapshotKindMismatchError — прежние версии другого вида.
        """
        current = await self._synced_or_none(cur, connection_id)
        if current is None:
            return

        if current.kind != kind:
            raise SnapshotKindMismatchError(connection_id, current.kind, kind)

    async def _synced_or_none(
        self, cur: Cursor, connection_id: UUID
    ) -> SyncedConnection | None:
        await cur.execute(
            self._latest_select(" where {cv_connection_id} = %(connection_id)s"),
            {"connection_id": connection_id},
        )
        row = await cur.fetchone()
        if row is None:
            return None

        return SyncedConnection.of(self._parse(ConnectionVersion, row))

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

    LATEST_SELECT: ClassVar[LiteralString] = """
        select distinct on ({cv_connection_id})
               {cv_connection_id}, {cv_version}, {cv_connection_name}, {cv_kind},
               {cv_taken_at}, {cv_taken_by}, {cv_sync_id}, {cv_objects_total},
               {cv_server_version}
        from {connection_versions}
        """
    LATEST_ORDER: ClassVar[LiteralString] = (
        " order by {cv_connection_id}, {cv_version} desc"
    )

    def _latest_select(self, where: LiteralString) -> sql.Composed:
        """Последняя версия каждого подключения под условием where."""
        return sql.Composed(
            [
                self._sql(self.LATEST_SELECT),
                self._sql(where),
                self._sql(self.LATEST_ORDER),
            ]
        )

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

        return self._parse(ConnectionVersion, row)

    async def _insert_snapshot(
        self, cur: Cursor, connection_id: UUID, version: int, snapshot: SourceSnapshot
    ) -> None:
        for spec in self._tables.of_kind(snapshot.kind):
            rows: list[dict[str, Any]] = []
            for row in spec.rows_of(snapshot):
                row[SnapshotKey.CONNECTION_ID.value] = connection_id
                row[SnapshotKey.VERSION.value] = version
                rows.append(row)

            if not rows:
                continue

            await cur.executemany(spec.insert(self._schema), rows)
