"""Приёмник pg_stream_in: стратегии схемы, удаления и вставки на потоке
Arrow из postgres (круг), из Oracle и из ClickHouse на новейшем postgres;
стратегии схемы — на каждой версии postgres и Greenplum стенда."""

# ruff: noqa: S608, PLR0913

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from boba.db.postgres.connection import CopyOptions
from boba.db.postgres.transfer import PgColumnDeclaration
from boba.pump_stand import Leg, Loaded, OracleSide, PostgresSide, Pumps, PumpStand
from boba.stand.names import StandNames
from boba.pump_stand.oracle import PumpUser
from boba.toolkit.transfer import (
    BackupAndCreateIfSchemaChanged,
    ColumnRules,
    CreateIfNotExists,
    DeleteAll,
    DeleteNothing,
    DeleteTruncate,
    DeleteWhere,
    DropAndCreateIfSchemaChanged,
    ErrorIfNotExists,
    ErrorIfSchemaChanged,
    InsertFull,
    InsertNothing,
    StreamWire,
    TransferError,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 60
CHUNK_BYTES = 4096
PG_SCHEMA = StandNames().of("pump_sync")
ARRAYSIZE = 500

SELECT = (
    "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, "
    "('name ' || g)::varchar(50) as name, "
    "timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, "
    "g % 2 = 0 as flag, g::float8 / 3 as d "
    f"from generate_series(1, {ROWS}) g"
)
"""Поток-источник: шесть семейств типов, тысяча строк."""


def _newest(sources: Sequence[Any]) -> str:
    """Имя новейшего PostgreSQL стенда (Greenplum не в счёт)."""
    plain: list[str] = []
    for source in sources:
        if source.name.startswith("pg-"):
            plain.append(source.name)

    return plain[-1]


NEWEST = _newest(STAND.sources)


@pytest.fixture(
    scope="module",
    params=[StreamWire.ARROW, StreamWire.CSV],
    ids=["arrow", "copy"],
)
def mode(request: Any) -> StreamWire:
    """Раскладка провода pg -> pg: arrow (нейтральный путь) или csv (пара)."""
    return request.param


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def postgres(request: Any, mode: StreamWire) -> AsyncIterator[PostgresSide]:
    """Схема пересоздаётся на каждую пару (режим провода, сервер)."""
    side = PostgresSide(request.param, PG_SCHEMA)
    await side.connect()
    await side.recreate_schema()
    yield side
    await side.drop()


@pytest.fixture(scope="module")
async def oracle(mode: StreamWire) -> AsyncIterator[OracleSide]:
    """Пользователь стенда пересоздаётся на каждый режим провода."""
    side = OracleSide(max(STAND.ora_sources, key=lambda s: s.name), ARRAYSIZE)
    await side.connect()
    await side.recreate_user()
    yield side
    await side.drop()


async def sync(
    postgres: PostgresSide,
    mode: StreamWire,
    table: str,
    schema: Any,
    delete: Any = DeleteNothing(kind="nothing"),
    insert: Any = InsertFull(kind="full"),
    rules: ColumnRules = ColumnRules(),
    select: str = SELECT,
    columns: Sequence[PgColumnDeclaration] = (),
) -> str:
    pumps = Pumps(postgres=postgres.profile)
    chained = await pumps.chain(
        Leg(
            "pg_stream_out",
            {
                "sql": select,
                "wire": mode,
                "columns": columns,
                "copy_options": CopyOptions(chunk_bytes=CHUNK_BYTES),
            },
        ),
        Leg(
            "pg_stream_in",
            {
                "schema_name": PG_SCHEMA,
                "table_name": table,
                "schema_strategy": schema,
                "delete_strategy": delete,
                "insert_strategy": insert,
                "rules": rules,
                "copy_options": CopyOptions(
                    chunk_bytes=CHUNK_BYTES, exact_floats=postgres.greenplum_6
                ),
            },
        ),
    )

    return chained.in_report


class TestSchemaStrategies:
    async def test_create_if_not_exists_builds_the_table_from_the_stream(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        report = await sync(
            postgres, mode, "fresh", CreateIfNotExists(kind="create_if_not_exists")
        )
        loaded = Loaded(postgres, PG_SCHEMA, "fresh")

        assert report.startswith(f"{ROWS} rows written into {PG_SCHEMA}.fresh")
        assert "schema: create (table is missing)" in report
        assert await loaded.columns() == [
            ("id", "bigint", False),
            ("amount", "numeric(18,4)", False),
            ("name", "character varying(50)", False),
            ("ts", "timestamp without time zone", False),
            ("flag", "boolean", False),
            ("d", "double precision", False),
        ]
        assert await loaded.count() == ROWS

    async def test_second_run_keeps_the_table_and_appends(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        await sync(
            postgres, mode, "twice", CreateIfNotExists(kind="create_if_not_exists")
        )
        report = await sync(
            postgres,
            mode,
            "twice",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
        )

        assert "schema: keep (schema matches)" in report
        assert await Loaded(postgres, PG_SCHEMA, "twice").count() == 2 * ROWS

    async def test_error_if_not_exists_refuses_a_missing_table(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        with pytest.raises(TransferError, match="table is missing"):
            await sync(
                postgres, mode, "absent", ErrorIfNotExists(kind="error_if_not_exists")
            )

    async def test_narrowed_column_is_a_schema_change(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        await postgres.create(
            "narrow",
            [
                "id bigint",
                "amount numeric(10,2)",
                "name varchar(50)",
                "ts timestamp(6)",
                "flag boolean",
                "d double precision",
            ],
        )
        with pytest.raises(TransferError, match="amount"):
            await sync(
                postgres,
                mode,
                "narrow",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            )

    async def test_backup_and_create_renames_the_old_table(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        await postgres.create("keep_old", ["id bigint", "extra text"])
        report = await sync(
            postgres,
            mode,
            "keep_old",
            BackupAndCreateIfSchemaChanged(kind="backup_and_create_if_schema_changed"),
        )
        loaded = Loaded(postgres, PG_SCHEMA, "keep_old")
        tables = await loaded.tables()

        assert "schema: backup_then_create" in report
        assert "backup: keep_old_bak_" in report
        assert [t for t in tables if t.startswith("keep_old_bak_")]
        assert await loaded.count() == ROWS

    async def test_drop_and_create_replaces_the_old_table(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        await postgres.create("replaced", ["id bigint", "extra text"])
        report = await sync(
            postgres,
            mode,
            "replaced",
            DropAndCreateIfSchemaChanged(kind="drop_and_create_if_schema_changed"),
        )
        loaded = Loaded(postgres, PG_SCHEMA, "replaced")

        assert "schema: drop_then_create" in report
        assert [c[0] for c in await loaded.columns()] == [
            "id",
            "amount",
            "name",
            "ts",
            "flag",
            "d",
        ]
        assert not [t for t in await loaded.tables() if t.startswith("replaced_bak_")]


class TestDeleteAndInsert:
    @pytest.fixture(autouse=True)
    def only_newest(self, postgres: PostgresSide) -> None:
        if postgres.source.name != NEWEST:
            pytest.skip("delete and insert strategies on the newest postgres")

    async def test_truncate_then_full(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        await sync(
            postgres, mode, "trunc", CreateIfNotExists(kind="create_if_not_exists")
        )
        report = await sync(
            postgres,
            mode,
            "trunc",
            CreateIfNotExists(kind="create_if_not_exists"),
            delete=DeleteTruncate(kind="truncate"),
        )

        assert "deleted: 0 rows by truncate table" in report
        assert await Loaded(postgres, PG_SCHEMA, "trunc").count() == ROWS

    async def test_delete_all_counts_rows(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        await sync(
            postgres, mode, "wipe", CreateIfNotExists(kind="create_if_not_exists")
        )
        report = await sync(
            postgres,
            mode,
            "wipe",
            CreateIfNotExists(kind="create_if_not_exists"),
            delete=DeleteAll(kind="delete_all"),
        )

        assert f"deleted: {ROWS} rows by delete from" in report
        assert await Loaded(postgres, PG_SCHEMA, "wipe").count() == ROWS

    async def test_delete_where_then_nothing_inserted(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        await sync(
            postgres, mode, "part", CreateIfNotExists(kind="create_if_not_exists")
        )
        half = ROWS // 2
        await sync(
            postgres,
            mode,
            "part",
            CreateIfNotExists(kind="create_if_not_exists"),
            delete=DeleteWhere(kind="delete_where", where=f"id <= {half}"),
        )

        assert await Loaded(postgres, PG_SCHEMA, "part").count() == 2 * ROWS - half

        report = await sync(
            postgres,
            mode,
            "part",
            CreateIfNotExists(kind="create_if_not_exists"),
            delete=DeleteWhere(kind="delete_where", where=f"id between 1 and {ROWS}"),
            insert=InsertNothing(kind="nothing"),
        )

        assert f"deleted: {2 * ROWS - half} rows" in report
        assert "0 rows written" in report
        assert await Loaded(postgres, PG_SCHEMA, "part").count() == 0

    async def test_rename_and_declaration_shape_the_ddl(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        rules = ColumnRules(
            rename_columns={"title": "name"}, column_types={"amount": "numeric(20,6)"}
        )
        await sync(
            postgres,
            mode,
            "shaped",
            CreateIfNotExists(kind="create_if_not_exists"),
            rules=rules,
        )
        columns = await Loaded(postgres, PG_SCHEMA, "shaped").columns()

        assert ("title", "character varying(50)", False) in columns
        assert ("amount", "numeric(20,6)", False) in columns
        assert await Loaded(postgres, PG_SCHEMA, "shaped").count() == ROWS


class TestDeclarations:
    """Декларации columns у источника несут not null и тип в контракт:
    приёмник создаёт таблицу с теми же ограничениями."""

    @pytest.fixture(autouse=True)
    def only_newest(self, postgres: PostgresSide) -> None:
        if postgres.source.name != NEWEST:
            pytest.skip("declarations on the newest postgres")

    async def test_pg_declarations_carry_not_null(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        await postgres.create(
            "strict_src",
            ["id bigint not null", "note text", "amount numeric(12,2) not null"],
        )
        await postgres.execute(
            [
                f"insert into {PG_SCHEMA}.strict_src "
                "select g, 'n' || g, g / 3.0 from generate_series(1, 100) g"
            ]
        )
        report = await sync(
            postgres,
            mode,
            "strict_dst",
            CreateIfNotExists(kind="create_if_not_exists"),
            select=f"select * from {PG_SCHEMA}.strict_src where id <= 50",
            columns=[
                PgColumnDeclaration(name="id", nullable=False),
                PgColumnDeclaration(name="amount", nullable=False),
            ],
        )

        assert report.startswith("50 rows written")
        assert await Loaded(postgres, PG_SCHEMA, "strict_dst").columns() == [
            ("id", "bigint", True),
            ("note", "text", False),
            ("amount", "numeric(12,2)", True),
        ]

    async def test_unknown_column_in_declarations_is_refused(
        self, postgres: PostgresSide, mode: StreamWire
    ) -> None:
        with pytest.raises(TransferError, match="has no column 'nope'"):
            await sync(
                postgres,
                mode,
                "never",
                CreateIfNotExists(kind="create_if_not_exists"),
                columns=[PgColumnDeclaration(name="nope", nullable=False)],
            )

    async def test_ora_declarations_carry_not_null(
        self, postgres: PostgresSide, oracle: OracleSide
    ) -> None:
        await oracle.create(
            "STRICT",
            [
                "ID number(10) not null",
                "NOTE varchar2(20)",
                "AMOUNT number(12,2) not null",
            ],
        )
        await oracle.run(
            (
                f"insert into {PumpUser().name}.strict select level, 'n' || level, "
                "level / 3 from dual connect by level <= 100",
            )
        )
        pumps = Pumps(postgres=postgres.profile, oracle=oracle.profile)
        chained = await pumps.chain(
            Leg(
                "ora_stream_out",
                {
                    "sql": f"select * from {PumpUser().name}.strict where id <= 50",
                    "columns": [
                        PgColumnDeclaration(name="ID", nullable=False),
                        PgColumnDeclaration(name="AMOUNT", nullable=False),
                    ],
                },
            ),
            Leg(
                "pg_stream_in",
                {
                    "schema_name": PG_SCHEMA,
                    "table_name": "strict_from_ora",
                    "schema_strategy": CreateIfNotExists(kind="create_if_not_exists"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "copy_options": CopyOptions(chunk_bytes=CHUNK_BYTES),
                },
            ),
        )

        assert chained.in_report.startswith("50 rows written")
        assert await Loaded(postgres, PG_SCHEMA, "strict_from_ora").columns() == [
            ("ID", "bigint", True),
            ("NOTE", "character varying(20)", False),
            ("AMOUNT", "numeric(12,2)", True),
        ]


class TestOtherSources:
    @pytest.fixture(autouse=True)
    def only_newest(self, postgres: PostgresSide) -> None:
        if postgres.source.name != NEWEST:
            pytest.skip("other sources on the newest postgres")

    async def test_oracle_stream_creates_exact_types(
        self, postgres: PostgresSide, mode: StreamWire, oracle: OracleSide
    ) -> None:
        await oracle.create(
            "src",
            [
                "id number(10)",
                "amount number(18,4)",
                "name varchar2(50)",
                "ts timestamp(6)",
                "rw raw(16)",
            ],
        )
        await oracle.run(
            (
                f"insert into {PumpUser().name}.src select level, level / 7, "
                "'n' || level, systimestamp, hextoraw('00ff') from dual "
                "connect by level <= 100",
            )
        )
        pumps = Pumps(postgres=postgres.profile, oracle=oracle.profile)
        chained = await pumps.chain(
            Leg(
                "ora_stream_out",
                {
                    "sql": 'select id as "id", amount as "amount", name as "name", '
                    'ts as "ts", \'\\\\x\' || rawtohex(rw) as "rw" '
                    f"from {PumpUser().name}.src",
                    "columns": [],
                },
            ),
            Leg(
                "pg_stream_in",
                {
                    "schema_name": PG_SCHEMA,
                    "table_name": "from_oracle",
                    "schema_strategy": CreateIfNotExists(kind="create_if_not_exists"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "copy_options": CopyOptions(chunk_bytes=CHUNK_BYTES),
                },
            ),
        )
        columns = await Loaded(postgres, PG_SCHEMA, "from_oracle").columns()

        assert chained.in_report.startswith("100 rows written")
        assert ("id", "bigint", False) in columns
        assert ("amount", "numeric(18,4)", False) in columns
        assert ("name", "character varying(50)", False) in columns
        assert ("ts", "timestamp(6) without time zone", False) in columns
        assert ("rw", "character varying(35)", False) in columns
