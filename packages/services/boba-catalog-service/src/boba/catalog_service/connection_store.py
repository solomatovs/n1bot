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
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from boba.catalog import SourceDiff, SourceKinds, SourceSnapshot, TreeScope
from boba.catalog_service.config import CatalogConfig
from boba.catalog_service.records import (
    CatalogStoreError,
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
from boba.catalog_service.store_base import CatalogStoreBase
from boba.db.postgres import (
    Cursor,
    PgQuery,
    PgQueryBuilder,
    PostgresPool,
    PostgresSchema,
)
from boba.db.postgres.catalog import (
    CatalogDomain,
    DomainVersions,
    SnapshotKey,
    SnapshotOutcome,
    SnapshotReader,
    SnapshotTable,
    SnapshotTables,
    StagingTables,
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


class ConstraintKind(StrEnum):
    """Виды ограничений pg_constraint, которые снимает перевод таблиц снимков."""

    PRIMARY = "p"
    UNIQUE = "u"
    FOREIGN = "f"


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
    ({schema}), версии и синхронизации — в схеме приложения ({app}) рядом с
    ProcessStore; прав не знает. Подключение блокируется на время записи
    версии и старта синхронизации advisory-замком по его id."""

    LOCK_PREFIX: ClassVar[str] = "catalog.connection"
    LABEL: ClassVar[str] = "catalog connections"

    def __init__(
        self,
        cfg: CatalogConfig,
        kinds: SourceKinds,
        pool: PostgresPool | None = None,
    ) -> None:
        super().__init__(cfg, cfg.db_schema, pool)
        self._app = PostgresSchema(cfg.app_schema)
        self._kinds = kinds
        self._tables = SnapshotTables(kinds)
        self._domain = CatalogDomain(cfg.db_schema, self._tables)
        self._reader = SnapshotReader(cfg.db_schema, kinds)
        self._staging = StagingTables(cfg.db_schema)
        self._legacy_staging = StagingTables(cfg.app_schema)

    @property
    def kinds(self) -> SourceKinds:
        return self._kinds

    def _query(self) -> PgQueryBuilder:
        return PgQueryBuilder(
            schema=self._schema.ident,
            app=self._app.ident,
            version_columns=self._column_list(VersionsColumn),
            sync_columns=self._column_list(SyncsColumn),
        )

    def snapshot_class(self, kind: str) -> type[SourceSnapshot]:
        """Класс снимка вида подключения.

        Ошибки:
        UnknownSourceKindError — снимка этого вида нет в реестре.
        """
        if not self._kinds.known(kind):
            raise UnknownSourceKindError(kind, self._kinds.kinds())

        return self._kinds.snapshot_class(kind)

    async def setup(self) -> None:
        """Схемы и таблицы; повтор безвреден. Таблицы прежних выпусков
        переводятся на месте: таблицы приложения из схемы домена — в схему
        приложения, таблицы снимков — на суррогатный ключ без связи с
        версиями; иная раскладка — отказ с расхождением колонок."""
        await self._apply_ddl((), self._app.name)
        await self._migrate()
        await self._apply_ddl(self._ddl(), self._app.name)
        await self._check_layouts(self._domain.layouts())
        await self._check_layouts(self._app_layouts(), self._app.name)

        logger.info(
            "catalog connections ready: %s (domain), %s (app)",
            self.schema,
            self._app.name,
        )

    async def _migrate(self) -> None:
        """Перевод прежних выпусков на месте одной транзакцией."""
        async with self._transaction("migrate older releases") as cur:
            for table in ConnectionTable:
                await self._schema.move_table(cur.connection, table.value, self._app)

            for spec in self._tables.all():
                await self._migrate_snapshot_table(cur, spec)

            await self._legacy_staging.drop_matching(cur, LegacyStaging.PATTERN)

    async def _migrate_snapshot_table(self, cur: Cursor, spec: SnapshotTable) -> None:
        """Таблица снимка прежнего выпуска: внешние ключи снимаются, ключ
        строки — bigserial id, прежний ключ версии — unique."""
        if not await self._schema.has_table(cur.connection, spec.table):
            return

        table = spec.ident(self.schema)
        for name in await self._constraints(cur, spec.table, ConstraintKind.FOREIGN):
            await self._drop_constraint(cur, table, name)

        id_type = (
            self._query()
            .add(
                """
                select data_type
                from information_schema.columns
                where table_schema = %(table_schema)s
                  and table_name = %(table)s
                  and column_name = %(column)s
                """,
                table_schema=self.schema,
                table=spec.table,
                column=SnapshotKey.ID.value,
            )
            .build()
        )
        await cur.execute(id_type.text, id_type.params)
        id_column = await cur.fetchone()
        if id_column is not None and id_column["data_type"] == "bigint":
            return

        for name in await self._constraints(cur, spec.table, ConstraintKind.PRIMARY):
            await self._drop_constraint(cur, table, name)

        if id_column is not None:
            drop_id = (
                PgQueryBuilder(table=table, column=sql.Identifier(SnapshotKey.ID.value))
                .add("alter table {table} drop column {column}")
                .build()
            )
            await cur.execute(drop_id.text, drop_id.params)

        add_id = (
            PgQueryBuilder(table=table, column=sql.Identifier(SnapshotKey.ID.value))
            .add("alter table {table} add column {column} bigserial primary key")
            .build()
        )
        await cur.execute(add_id.text, add_id.params)

        if await self._constraints(cur, spec.table, ConstraintKind.UNIQUE):
            return

        add_key = (
            PgQueryBuilder(
                table=table,
                constraint=sql.Identifier(f"{spec.table}_key"),
                key=spec.native_key(),
            )
            .add("alter table {table} add constraint {constraint} unique ({key})")
            .build()
        )
        await cur.execute(add_key.text, add_key.params)

    async def _drop_constraint(
        self, cur: Cursor, table: sql.Identifier, name: str
    ) -> None:
        drop = (
            PgQueryBuilder(table=table, constraint=sql.Identifier(name))
            .add("alter table {table} drop constraint {constraint}")
            .build()
        )
        await cur.execute(drop.text, drop.params)

    async def _constraints(
        self, cur: Cursor, table: str, kind: ConstraintKind
    ) -> list[str]:
        """Имена ограничений таблицы домена данного вида."""
        query = (
            self._query()
            .add(
                """
                select c.conname
                from pg_constraint c
                    join pg_class r on r.oid = c.conrelid
                    join pg_namespace n on n.oid = r.relnamespace
                where c.contype = %(kind)s
                  and n.nspname = %(nspname)s and r.relname = %(table)s
                """,
                kind=kind.value,
                nspname=self.schema,
                table=table,
            )
            .build()
        )
        await cur.execute(query.text, query.params)
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

    def _ddl(self) -> tuple[PgQuery, ...]:
        statements: list[PgQuery] = [
            self._query()
            .add(
                """
                create table if not exists {app}.connection_syncs (
                    id              uuid primary key,
                    connection_id   uuid not null,
                    connection_name text not null,
                    kind            text not null,
                    started_by      uuid not null,
                    started_at      timestamptz not null default now(),
                    finished_at     timestamptz null,
                    status          text not null,
                    scope           jsonb not null default '{{}}'::jsonb,
                    objects_total   integer null,
                    objects_done    integer not null default 0,
                    error           text null,
                    version         integer null
                )
                """
            )
            .build(),
            self._query()
            .add(
                """
                create table if not exists {app}.connection_versions (
                    connection_id   uuid not null,
                    version         integer not null,
                    connection_name text not null,
                    kind            text not null,
                    taken_at        timestamptz not null default now(),
                    taken_by        uuid not null,
                    sync_id         uuid null
                                    references {app}.connection_syncs (id),
                    objects_total   integer not null default 0,
                    server_version  text null,
                    primary key (connection_id, version)
                )
                """
            )
            .build(),
        ]
        statements.extend(self._domain.ddl())
        return tuple(statements)

    # --- подключения глазами каталога ---

    async def synced_connections(self) -> Sequence[SyncedConnection]:
        """Подключения с версиями снимка по последней версии каждого."""
        query = (
            self._query()
            .add(
                """
                select * from (
                    select distinct on (connection_id)
                        {version_columns}
                    from {app}.connection_versions
                    order by connection_id, version desc
                ) latest
                order by connection_name, connection_id
                """
            )
            .build()
        )
        rows = await self._rows(query, "list synced connections")

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
            await self._lock(cur, self.LOCK_PREFIX, connection_id)
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
        latest = await self._latest_domain_version(cur, type(snapshot), connection_id)
        version = latest + 1
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

    def _domain_versions(self, snapshot: type[SourceSnapshot]) -> DomainVersions:
        """Версии подключения по корневой таблице снимка этого вида."""
        root = self._tables.of_snapshot(snapshot)[0]

        return DomainVersions(self.schema, root.table)

    def _family_tables(self, snapshot: type[SourceSnapshot]) -> list[str]:
        """Таблицы семейств снимка: по ним считаются объекты версии.

        Ошибки:
        CatalogStoreError — семейство ссылается на часть, которой нет у снимка.
        """
        specs = self._tables.of_snapshot(snapshot)
        tables: list[str] = []
        for family in snapshot.families():
            tables.append(self._table_of(specs, snapshot, family.part))

        return tables

    def _table_of(
        self,
        specs: Sequence[SnapshotTable],
        snapshot: type[SourceSnapshot],
        part: str,
    ) -> str:
        for spec in specs:
            if spec.part.name == part:
                return spec.table

        msg = (
            f"{self.LABEL}: snapshot of a {snapshot.source_kind()} source has no "
            f"part {part!r}"
        )
        raise CatalogStoreError(msg)

    async def _latest_domain_version(
        self, cur: Cursor, snapshot: type[SourceSnapshot], connection_id: UUID
    ) -> int:
        """Последняя версия подключения по строкам домена; 0 — строк нет."""
        query = self._domain_versions(snapshot).latest(connection_id)
        await cur.execute(query.text, query.params)
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
        query = (
            self._query()
            .add(
                """
                insert into {app}.connection_versions
                    (connection_id, version, connection_name, kind,
                     taken_by, sync_id, objects_total,
                     server_version)
                values
                    (%(connection_id)s, %(version)s, %(connection_name)s, %(kind)s,
                     %(taken_by)s, %(sync_id)s, %(objects_total)s, %(server_version)s)
                """,
                connection_id=connection_id,
                version=header.version,
                connection_name=origin.connection_name,
                kind=header.kind,
                taken_by=origin.taken_by,
                sync_id=origin.sync_id,
                objects_total=header.objects_total,
                server_version=origin.server_version,
            )
            .build()
        )
        await cur.execute(query.text, query.params)

    async def versions_of(self, connection_id: UUID) -> Sequence[ConnectionVersion]:
        query = (
            self._version_query()
            .add(
                "where connection_id = %(connection_id)s order by version",
                connection_id=connection_id,
            )
            .build()
        )
        rows = await self._rows(query, f"versions of connection {connection_id}")

        return self._parse_all(ConnectionVersion, rows)

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
            await self._lock(cur, self.LOCK_PREFIX, connection_id)
            running = await self._running_sync(cur, connection_id)
            if running is not None:
                raise SyncRunningError(connection_id, running.id)

            synced = await self._synced_or_none(cur, connection_id)
            if synced is not None:
                for spec in self._tables.of_kind(synced.kind):
                    await self._delete_rows(cur, spec, connection_id)

            await self._staging.drop_of(cur, connection_id)
            versions = (
                self._query()
                .add(
                    """
                    delete from {app}.connection_versions
                    where connection_id = %(connection_id)s
                    """,
                    connection_id=connection_id,
                )
                .build()
            )
            await cur.execute(versions.text, versions.params)
            return cur.rowcount

    async def _delete_rows(
        self, cur: Cursor, spec: SnapshotTable, connection_id: UUID
    ) -> None:
        """Строки всех версий подключения в таблице части."""
        query = (
            PgQueryBuilder(table=spec.ident(self.schema))
            .add(
                "delete from {table} where connection_id = %(connection_id)s",
                connection_id=connection_id,
            )
            .build()
        )
        await cur.execute(query.text, query.params)

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
        insert = (
            self._query()
            .add(
                """
                insert into {app}.connection_syncs
                    (id, connection_id, connection_name, kind,
                     started_by, status, scope)
                values
                    (%(id)s, %(connection_id)s, %(connection_name)s, %(kind)s,
                     %(started_by)s, %(status)s, %(scope)s)
                """,
                id=sync_id,
                connection_id=connection_id,
                connection_name=request.connection.name,
                kind=request.connection.kind,
                started_by=started_by,
                status=SyncStatus.RUNNING.value,
                scope=Jsonb(request.scope.model_dump(mode="json")),
            )
            .build()
        )

        action = f"start sync of connection {connection_id}"
        async with self._transaction(action) as cur:
            await self._lock(cur, self.LOCK_PREFIX, connection_id)
            await self._require_same_kind(cur, connection_id, request.connection.kind)

            running = await self._running_sync(cur, connection_id)
            if running is not None:
                raise SyncRunningError(connection_id, running.id)

            await cur.execute(insert.text, insert.params)
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
            await self._lock(cur, self.LOCK_PREFIX, sync.connection_id)
            snapshot_class = self._kinds.snapshot_class(sync.kind)
            connection_id = sync.connection_id
            latest = await self._latest_domain_version(
                cur, snapshot_class, connection_id
            )
            if latest < outcome.version:
                raise SyncOutcomeError(sync_id, outcome.version, latest)

            objects = await self._domain_objects(
                cur, snapshot_class, connection_id, outcome.version
            )
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
            close = (
                self._query()
                .add(
                    """
                    update {app}.connection_syncs
                    set status = %(status)s,
                        finished_at = now(),
                        objects_total = %(objects)s,
                        objects_done = %(objects)s,
                        version = %(version)s
                    where id = %(id)s
                    """,
                    id=sync_id,
                    status=SyncStatus.DONE.value,
                    objects=objects,
                    version=outcome.version,
                )
                .build()
            )
            await cur.execute(close.text, close.params)
            return await self._sync(cur, sync_id)

    async def _domain_objects(
        self,
        cur: Cursor,
        snapshot: type[SourceSnapshot],
        connection_id: UUID,
        version: int,
    ) -> int:
        tables = self._family_tables(snapshot)
        query = self._domain_versions(snapshot).objects(tables, connection_id, version)
        await cur.execute(query.text, query.params)
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
        close = (
            self._query()
            .add(
                """
                update {app}.connection_syncs
                set status = %(status)s,
                    finished_at = now(),
                    error = %(error)s
                where id = %(id)s
                """,
                id=sync_id,
                status=status.value,
                error=error,
            )
            .build()
        )

        async with self._transaction(f"close sync {sync_id} as {status.value}") as cur:
            sync = await self._sync(cur, sync_id, lock=True)
            self._require_running(sync)
            await cur.execute(close.text, close.params)
            return await self._sync(cur, sync_id)

    async def get_sync(self, sync_id: UUID) -> Sync:
        async with self._transaction(f"get sync {sync_id}") as cur:
            return await self._sync(cur, sync_id)

    async def syncs_of(self, connection_id: UUID) -> Sequence[Sync]:
        """Синхронизации подключения, новые первыми."""
        query = (
            self._sync_query()
            .add(
                "where connection_id = %(connection_id)s order by started_at desc",
                connection_id=connection_id,
            )
            .build()
        )
        rows = await self._rows(query, f"syncs of connection {connection_id}")

        return self._parse_all(Sync, rows)

    # --- внутреннее: синхронизации ---

    def _sync_query(self) -> PgQueryBuilder:
        """Строки синхронизаций; условие вызывающий добавляет следующим куском."""
        return self._query().add("select {sync_columns} from {app}.connection_syncs")

    async def _sync(self, cur: Cursor, sync_id: UUID, *, lock: bool = False) -> Sync:
        query = self._sync_query().add("where id = %(id)s", id=sync_id)
        query.when(lock, "for update")
        built = query.build()

        await cur.execute(built.text, built.params)
        row = await cur.fetchone()
        if row is None:
            raise SyncNotFoundError(sync_id)

        return self._parse(Sync, row)

    async def _running_sync(self, cur: Cursor, connection_id: UUID) -> Sync | None:
        query = (
            self._sync_query()
            .add(
                "where connection_id = %(connection_id)s and status = %(status)s",
                connection_id=connection_id,
                status=SyncStatus.RUNNING.value,
            )
            .build()
        )
        await cur.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None:
            return None

        return self._parse(Sync, row)

    def _require_running(self, sync: Sync) -> None:
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
        query = (
            self._query()
            .add(
                """
                select distinct on (connection_id)
                    {version_columns}
                from {app}.connection_versions
                where connection_id = %(connection_id)s
                order by connection_id, version desc
                """,
                connection_id=connection_id,
            )
            .build()
        )
        await cur.execute(query.text, query.params)
        row = await cur.fetchone()
        if row is None:
            return None

        return SyncedConnection.of(self._parse(ConnectionVersion, row))

    async def _synced(self, cur: Cursor, connection_id: UUID) -> SyncedConnection:
        synced = await self._synced_or_none(cur, connection_id)
        if synced is None:
            raise ConnectionNotSyncedError(connection_id)

        return synced

    def _version_query(self) -> PgQueryBuilder:
        """Строки версий; условие вызывающий добавляет следующим куском."""
        return self._query().add(
            "select {version_columns} from {app}.connection_versions"
        )

    async def _version(
        self, cur: Cursor, connection_id: UUID, version: int
    ) -> ConnectionVersion:
        query = (
            self._version_query()
            .add(
                "where connection_id = %(connection_id)s and version = %(version)s",
                connection_id=connection_id,
                version=version,
            )
            .build()
        )
        await cur.execute(query.text, query.params)
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

            insert = spec.insert(self.schema)
            await cur.executemany(insert.text, rows)
