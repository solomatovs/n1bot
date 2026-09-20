"""Хранилище снимков подключений на живом Postgres: запись версии целиком
и чтение обратно байт-в-байт, версии и diff, список синхронизированных
подключений, вид зафиксирован первой версией, версии забываются вместе со
строками; версия, положенная в домен инструментом снятия (SnapshotWriter),
записывается по его итогу."""

from __future__ import annotations

from uuid import UUID

import pytest
from psycopg import sql

from boba.catalog import (
    ChangeStatus,
    ObjectKind,
    ObjectRef,
    SourceRecord,
    SourceSnapshot,
)
from boba.catalog_service import (
    ConnectionInfo,
    ConnectionNotSyncedError,
    ConnectionStore,
    ConnectionVersionNotFoundError,
    SnapshotKindMismatchError,
    SyncOutcomeError,
    SyncRequest,
    SyncStatus,
    VersionOrigin,
)
from boba.db.clickhouse.snapshot import ChSnapshot
from boba.db.clickhouse.snapshot_sample import ChSample
from boba.db.postgres import AsyncPostgresPool, PayloadPostgres
from boba.db.postgres.catalog import (
    CatalogDomainError,
    PartTable,
    SnapshotOutcome,
    SnapshotTables,
    SnapshotWriter,
    StagingTable,
)
from boba.db.postgres.profile import PostgresConfig
from boba.db.postgres.snapshot import PgSnapshot
from boba.db.postgres.snapshot_sample import PgSample
from boba.stand.catalog_stand import CatalogStand

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

CONFIG = CatalogStand.config("catalog_connections_test", ("viewer",), ("editor",))
SCHEMA = CONFIG.db_schema
APP_SCHEMA = CONFIG.app_schema
ADMIN = UUID(int=71)
PROD = UUID(int=72)
DWH = UUID(int=73)


def _origin(name: str, server_version: str | None = None) -> VersionOrigin:
    return VersionOrigin(
        taken_by=ADMIN, connection_name=name, server_version=server_version
    )


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ConnectionStore:
    stand = await CatalogStand.build(pool, CONFIG, CatalogStand.kinds())
    await stand.connections.setup()
    return stand.connections


async def test_postgres_version_round_trips(store: ConnectionStore) -> None:
    sample = PgSample()
    assert await store.synced_connections() == []
    assert await store.synced_or_none(PROD) is None
    with pytest.raises(ConnectionNotSyncedError):
        await store.snapshot_of(PROD, 0)

    version = await store.write_version(
        PROD, sample.snapshot(), _origin("pg-prod", "16.3")
    )
    assert version.version == 1
    assert version.connection_name == "pg-prod"
    assert version.kind == "postgres"
    assert version.objects_total == 9
    assert version.server_version == "16.3"

    synced = await store.synced(PROD)
    assert synced.name == "pg-prod"
    assert synced.kind == "postgres"
    assert synced.latest_version == 1

    stored = await store.snapshot_of(PROD, 1)
    assert isinstance(stored, PgSnapshot)
    assert _sorted(stored) == _sorted(sample.snapshot())

    assert await store.snapshot_of(PROD, 0) == PgSnapshot.empty()
    with pytest.raises(ConnectionVersionNotFoundError):
        await store.snapshot_of(PROD, 2)

    # имя подключения на момент снятия: переименованное попадает в новую версию
    second = await store.write_version(
        PROD, sample.next_version(), _origin("pg-prod-main")
    )
    assert second.version == 2
    assert [v.version for v in await store.versions_of(PROD)] == [1, 2]
    assert (await store.synced(PROD)).name == "pg-prod-main"
    latest = await store.latest_snapshot(PROD)
    assert _sorted(latest) == _sorted(sample.next_version())

    diff = await store.diff_of(PROD, 1, 2)
    removed = ObjectRef(
        connection_id=PROD,
        kind=ObjectKind.RELATION,
        path=("prod", "public", "customers"),
    )
    assert diff.status_of(removed) is ChangeStatus.REMOVED
    orders = ObjectRef(
        connection_id=PROD, kind=ObjectKind.RELATION, path=("prod", "public", "orders")
    )
    assert diff.status_of(orders) is ChangeStatus.MODIFIED


async def test_clickhouse_version_round_trips(store: ConnectionStore) -> None:
    sample = ChSample()

    await store.write_version(DWH, sample.snapshot(), _origin("ch-dwh"))
    stored = await store.snapshot_of(DWH, 1)
    assert isinstance(stored, ChSnapshot)
    assert _sorted(stored) == _sorted(sample.snapshot())

    tree = stored.children(DWH, ("dwh", "tables"))
    assert [node.label for node in tree] == ["events"]


async def test_snapshot_kind_is_fixed_by_the_first_version(
    store: ConnectionStore,
) -> None:
    await store.write_version(DWH, ChSample().snapshot(), _origin("ch-dwh"))
    with pytest.raises(SnapshotKindMismatchError, match="clickhouse"):
        await store.write_version(DWH, PgSample().snapshot(), _origin("ch-dwh"))


async def test_forget_versions_drops_rows_and_the_listing(
    store: ConnectionStore,
) -> None:
    await store.write_version(PROD, PgSample().snapshot(), _origin("pg-prod"))
    await store.write_version(PROD, PgSample().next_version(), _origin("pg-prod"))
    await store.write_version(DWH, ChSample().snapshot(), _origin("ch-dwh"))
    assert [s.name for s in await store.synced_connections()] == ["ch-dwh", "pg-prod"]

    assert await store.forget_versions(PROD) == 2
    assert await store.forget_versions(PROD) == 0
    assert [s.name for s in await store.synced_connections()] == ["ch-dwh"]
    with pytest.raises(ConnectionNotSyncedError):
        await store.latest_snapshot(PROD)

    # строки снимка ушли каскадом вместе с шапками версий
    again = await store.write_version(PROD, PgSample().snapshot(), _origin("pg-prod"))
    assert again.version == 1
    assert _sorted(await store.snapshot_of(PROD, 1)) == _sorted(PgSample().snapshot())


def _sorted(snapshot: SourceSnapshot) -> dict[str, list[SourceRecord]]:
    """Записи по частям, отсортированные по ключу: порядок строк из базы не
    гарантирован, а содержимое должно совпасть целиком."""
    tables: dict[str, list[SourceRecord]] = {}
    for part in snapshot.parts():
        records = list(snapshot.records_of(part.name))
        records.sort(key=lambda record: record.key)
        tables[part.name] = records

    return tables


async def _staging_tables(pool: AsyncPostgresPool, connection_id: UUID) -> list[str]:
    async with pool.connection() as conn, conn.cursor() as cur:
        pattern = StagingTable.pattern_of(connection_id)
        return await StagingTable.names_in(cur, SCHEMA, pattern)


def _part_tables() -> list[PartTable]:
    tables: list[PartTable] = []
    for spec in SnapshotTables.of_snapshot(PgSnapshot):
        tables.append(spec.part_table())

    return tables


def _rows(snapshot: SourceSnapshot, part: str) -> list[dict[str, object]]:
    spec = SnapshotTables.of_snapshot(type(snapshot))
    rows: list[dict[str, object]] = []
    for table in spec:
        if table.part.name != part:
            continue

        for record in snapshot.records_of(part):
            rows.append(table.row_of(record))

    return rows


async def test_tool_written_version_is_recorded_from_its_outcome(
    store: ConnectionStore, pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> None:
    """Инструмент снятия пишет строки в домен сам: staging заново, порции,
    перенос версией одной транзакцией; хранилище по итогу записывает шапку
    версии с числом объектов по строкам домена и закрывает синхронизацию.
    Повтор ключа в порции отказывает; версия, которой в домене нет, — отказ
    записи."""
    sample = PgSample()
    snapshot = sample.snapshot()
    connection = ConnectionInfo(id=PROD, name="prod", kind="postgres")
    request = SyncRequest(connection=connection)
    sync = await store.start_sync(UUID(int=0x5100), request, ADMIN)

    conn = await PayloadPostgres.connect_config(test_postgres)
    async with conn:
        writer = SnapshotWriter(conn, SCHEMA, PROD, _part_tables())
        await writer.open()
        assert len(await _staging_tables(pool, PROD)) == len(PgSnapshot.parts())

        for part in snapshot.parts():
            await writer.stage(part.name, _rows(snapshot, part.name))

        early = SnapshotOutcome(version=1, server_version="16")
        with pytest.raises(SyncOutcomeError, match="reports version 1"):
            await store.record_sync(sync.id, early)

        version = await writer.commit()

    assert version == 1
    assert await _staging_tables(pool, PROD) == []

    recorded = await store.record_sync(
        sync.id, SnapshotOutcome(version=1, server_version="16")
    )
    assert recorded.status is SyncStatus.DONE
    assert recorded.version == 1
    assert recorded.objects_total == snapshot.objects_count()
    assert recorded.objects_done == snapshot.objects_count()

    header = await store.version_of(PROD, 1)
    assert header.sync_id == sync.id
    assert header.server_version == "16"
    assert header.objects_total == snapshot.objects_count()
    assert _sorted(await store.snapshot_of(PROD, 1)) == _sorted(snapshot)

    conn = await PayloadPostgres.connect_config(test_postgres)
    async with conn:
        writer = SnapshotWriter(conn, SCHEMA, PROD, _part_tables())
        await writer.open()
        for part in snapshot.parts():
            await writer.stage(part.name, _rows(snapshot, part.name))

        await writer.stage("databases", _rows(snapshot, "databases"))
        with pytest.raises(CatalogDomainError, match="moving staging"):
            await writer.commit()

    stand = await store.write_version(PROD, sample.next_version(), _origin("prod"))
    assert stand.version == 2


async def _primary_key(pool: AsyncPostgresPool, table: str) -> list[str]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            select a.attname
            from pg_constraint c
                join pg_class r on r.oid = c.conrelid
                join pg_namespace n on n.oid = r.relnamespace
                cross join lateral unnest(c.conkey) with ordinality as k(attnum, n)
                join pg_attribute a on a.attrelid = r.oid and a.attnum = k.attnum
            where c.contype = 'p' and n.nspname = %s and r.relname = %s
            order by k.n
            """,
            (SCHEMA, table),
        )
        rows = await cur.fetchall()

    return [str(row[0]) for row in rows]


async def _column_type(pool: AsyncPostgresPool, table: str, column: str) -> str:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select data_type from information_schema.columns "
            "where table_schema = %s and table_name = %s and column_name = %s",
            (SCHEMA, table, column),
        )
        row = await cur.fetchone()

    if row is None:
        return ""

    return str(row[0])


async def _constraint_kinds(pool: AsyncPostgresPool, table: str) -> set[str]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            select c.contype from pg_constraint c
                join pg_class r on r.oid = c.conrelid
                join pg_namespace n on n.oid = r.relnamespace
            where n.nspname = %s and r.relname = %s
            """,
            (SCHEMA, table),
        )
        rows = await cur.fetchall()

    return {str(row[0]) for row in rows}


async def test_snapshot_tables_of_older_releases_are_migrated_in_place(
    store: ConnectionStore, pool: AsyncPostgresPool
) -> None:
    """Таблица снимка выпуска с составным первичным ключом и внешним ключом
    на версии, и таблица выпуска с uuid id переводятся при старте на месте:
    id bigserial первичным ключом, прежний ключ версии — unique, внешних
    ключей нет; строки переживают перевод, повторный старт безвреден."""
    assert await _primary_key(pool, "pg_databases") == ["id"]
    assert await _column_type(pool, "pg_databases", "id") == "bigint"
    assert await _constraint_kinds(pool, "pg_databases") == {"p", "u"}

    table = sql.Identifier(SCHEMA, "pg_databases")
    versions = sql.Identifier(APP_SCHEMA, "connection_versions")
    async with pool.connection() as conn:
        await conn.execute(sql.SQL("drop table {} cascade").format(table))
        await conn.execute(
            sql.SQL(
                """
                create table {} (
                    connection_id uuid not null,
                    version integer not null,
                    name text, owner text, encoding text, "collate" text,
                    comment text,
                    primary key (connection_id, version, name),
                    foreign key (connection_id, version)
                        references {} (connection_id, version) on delete cascade
                )
                """
            ).format(table, versions)
        )
        await conn.execute(
            sql.SQL(
                "insert into {} (connection_id, version, connection_name, kind, "
                "taken_by, objects_total) values (%s, 1, 'prod', 'postgres', %s, 1)"
            ).format(versions),
            (PROD, ADMIN),
        )
        await conn.execute(
            sql.SQL(
                "insert into {} values (%s, 1, 'prod', 'app', 'UTF8', 'C', null)"
            ).format(table),
            (PROD,),
        )

    old = await _primary_key(pool, "pg_databases")
    assert old == ["connection_id", "version", "name"]
    await store.setup()
    assert await _primary_key(pool, "pg_databases") == ["id"]
    assert await _column_type(pool, "pg_databases", "id") == "bigint"
    assert await _constraint_kinds(pool, "pg_databases") == {"p", "u"}

    # выпуск с uuid id: колонка меняется на bigserial
    async with pool.connection() as conn:
        await conn.execute(sql.SQL("drop table {} cascade").format(table))
        await conn.execute(
            sql.SQL(
                """
                create table {} (
                    id uuid primary key default
                        uuid_in(md5(random()::text || random()::text)::cstring),
                    connection_id uuid not null,
                    version integer not null,
                    name text, owner text, encoding text, "collate" text,
                    comment text,
                    constraint pg_databases_key unique (connection_id, version, name)
                )
                """
            ).format(table)
        )
        await conn.execute(
            sql.SQL(
                "insert into {} (connection_id, version, name) values (%s, 1, 'prod')"
            ).format(table),
            (PROD,),
        )

    assert await _column_type(pool, "pg_databases", "id") == "uuid"
    await store.setup()
    await store.setup()
    assert await _primary_key(pool, "pg_databases") == ["id"]
    assert await _column_type(pool, "pg_databases", "id") == "bigint"

    async with pool.connection() as conn:
        cur = await conn.execute(sql.SQL("select id, name from {}").format(table))
        rows = await cur.fetchall()

    assert [(row[0], row[1]) for row in rows] == [(1, "prod")]


async def test_app_tables_move_from_the_domain_schema(
    store: ConnectionStore, pool: AsyncPostgresPool
) -> None:
    """Таблица приложения выпуска с одной схемой (connection_syncs в схеме
    домена) переезжает при старте в схему приложения вместе со строками."""
    old = sql.Identifier(SCHEMA, "connection_syncs")
    new = sql.Identifier(APP_SCHEMA, "connection_syncs")
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("alter table {} set schema {}").format(new, sql.Identifier(SCHEMA))
        )
        await conn.execute(
            sql.SQL(
                "insert into {} (id, connection_id, connection_name, kind, started_by, "
                "status, scope) values (%s, %s, 'prod', 'postgres', %s, 'done', '{{}}')"
            ).format(old),
            (UUID(int=0x5300), PROD, ADMIN),
        )

    await store.setup()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "select table_schema from information_schema.tables "
            "where table_name = 'connection_syncs' and table_schema in (%s, %s)",
            (SCHEMA, APP_SCHEMA),
        )
        schemas = [str(row[0]) for row in await cur.fetchall()]
        moved = await conn.execute(sql.SQL("select count(*) from {}").format(new))
        count = await moved.fetchone()

    assert schemas == [APP_SCHEMA]
    assert count is not None
    assert count[0] == 1


async def test_link_table_lives_in_the_domain_schema(pool: AsyncPostgresPool) -> None:
    """Таблица связей домена создаётся рядом с таблицами снимков: bigserial
    id, ноль вместо null у родителя, unique по четвёрке концов."""
    store = ConnectionStore(CONFIG, CatalogStand.kinds(), pool)
    await store.setup()
    link = sql.Identifier(SCHEMA, "link")
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL(
                "insert into {} (from_id, from_entity, to_id, to_entity) "
                "values (1, 1, 2, 1)"
            ).format(link)
        )
        cur = await conn.execute(sql.SQL("select id, parent_id from {}").format(link))
        row = await cur.fetchone()

    assert row is not None
    assert row[0] >= 1
    assert row[1] == 0
    assert await _primary_key(pool, "link") == ["id"]
