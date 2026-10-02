# ruff: noqa: S608
"""Насосы Oracle на потоке arrow: ora_stream_out как источник и ora_stream_in
как приёмник со стратегиями, на каждом Oracle стенда.

Что проверяется:
    - круг Oracle -> Oracle: таблица всех ходовых типов уезжает
      ora_stream_out и приходит ora_stream_in в новую таблицу теми же типами
      (текст типа источника в DDL), значения совпадают; повторная загрузка
      проходит сверку;
    - Oracle -> postgres и Oracle -> ClickHouse: приёмник создаёт таблицу по
      нейтральному контракту, целый NUMBER — bigint/Int64, NUMBER(p, s) —
      numeric/Decimal, DATE — момент в секундах;
    - postgres -> Oracle и ClickHouse -> Oracle: приёмник создаёт таблицу
      типами Oracle: uuid и time — VARCHAR2, json — CLOB, boolean — BOOLEAN
      или NUMBER(1), date — DATE, bytea — BLOB;
    - стратегии приёмника: delete_where, витрина через rename и column_types,
      дрейф схемы с бэкапом, шаблон create table, сухой прогон, отказ на
      таблице уже потока;
    - отказы источника до выполнения: TIMESTAMP WITH TIME ZONE, INTERVAL,
      XMLTYPE — с подсказкой, что писать в select.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from boba.db.clickhouse.target import ChStreamWire
from boba.db.oracle import OracleQueryError
from boba.db.oracle.target import OraTableRef
from boba.db.postgres.connection import CopyOptions
from boba.db.postgres.transfer import PgColumnDeclaration
from boba.pump_stand import (
    ChLoaded,
    ClickHouseSide,
    Leg,
    Loaded,
    OracleSide,
    OraLoaded,
    PostgresSide,
    Pumps,
    PumpStand,
)
from boba.pump_stand.oracle import PumpUser
from boba.stand.names import StandNames
from boba.toolkit.contract import ColumnDeclaration, ContractError
from boba.toolkit.transfer import (
    BackupAndCreateIfSchemaChanged,
    ColumnRules,
    CreateIfNotExists,
    DeleteNothing,
    DeleteTruncate,
    DeleteWhere,
    DoNothing,
    DropAndCreate,
    ErrorIfSchemaChanged,
    FailOnUnknown,
    FallbackAsVarchar,
    InsertFull,
    InsertNothing,
    StreamWire,
    TransferError,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 60
ARRAYSIZE = 500
CHUNK = 65536
PG_SCHEMA = StandNames().of("pump_ora_sync")
CH_DATABASE = StandNames().of("pump_ora_sync")
BOOLEAN_SINCE = 23
MERGE_TREE = (
    "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
    "engine = MergeTree order by {order_by}"
)
SRC = StandNames().of("src")
OWNER = PumpUser().name

TYPED_DDL = [
    "id number(10) not null",
    "n19 number(19)",
    "n18_4 number(18,4)",
    "nfree number",
    "f126 float(126)",
    "bd binary_double",
    "bf binary_float",
    "vc varchar2(40)",
    "vcc varchar2(20 char)",
    "nvc nvarchar2(20)",
    "c5 char(5)",
    "cl clob",
    "r16 raw(16)",
    "bl blob",
    "d date",
    "ts6 timestamp(6)",
    "ts9 timestamp(9)",
]
TYPED_FILL = f"""
insert into {OWNER}.{SRC}
select level,
       level * 1000000007,
       level / 7,
       level * 3,
       1 / 3,
       level / 7,
       level / 3,
       'v' || level || ' tab' || chr(9) || 'quote "q" \\ back',
       case when mod(level, 5) = 0 then null else 'c' || level end,
       'n' || level,
       'ab',
       to_clob(rpad('long ' || level, 5000, 'x')),
       hextoraw('00ff' || lpad(to_char(level, 'fmxxxx'), 4, '0')),
       to_blob(hextoraw('00ff10')),
       date '2024-01-01' + mod(level, 400),
       timestamp '2024-02-29 13:14:15.123456' + numtodsinterval(level, 'second'),
       timestamp '2024-02-29 13:14:15.123456789' + numtodsinterval(level, 'second')
from dual connect by level <= {ROWS}"""

SELECT = (
    f'select id as "id", n19 as "n19", n18_4 as "n18_4", nfree as "nfree", '
    f'f126 as "f126", bd as "bd", bf as "bf", vc as "vc", vcc as "vcc", '
    f'nvc as "nvc", c5 as "c5", r16 as "r16", d as "d", ts6 as "ts6", '
    f'ts9 as "ts9", cl as "cl", bl as "bl" from {OWNER}.{SRC}'
)
SELECT_TEXT_ONLY = (
    f'select id as "id", n19 as "n19", n18_4 as "n18_4", nfree as "nfree", '
    f'f126 as "f126", bd as "bd", bf as "bf", vc as "vc", vcc as "vcc", '
    f'nvc as "nvc", c5 as "c5", rawtohex(r16) as "r16", d as "d", '
    f'ts6 as "ts6", ts9 as "ts9", cl as "cl" from {OWNER}.{SRC}'
)
"""Для postgres: двоичные колонки текстом hex — COPY из arrow bytes не берёт."""

DECLARED = (ColumnDeclaration(name="id", nullable=False),)

EXPECTED_ORACLE = [
    ("id", "NUMBER(10,0)", True),
    ("n19", "NUMBER(19,0)", False),
    ("n18_4", "NUMBER(18,4)", False),
    ("nfree", "NUMBER", False),
    ("f126", "FLOAT(126)", False),
    ("bd", "BINARY_DOUBLE", False),
    ("bf", "BINARY_FLOAT", False),
    ("vc", "VARCHAR2(40)", False),
    ("vcc", "VARCHAR2(20 CHAR)", False),
    ("nvc", "NVARCHAR2(20)", False),
    ("c5", "CHAR(5)", False),
    ("r16", "RAW(16)", False),
    ("d", "DATE", False),
    ("ts6", "TIMESTAMP(6)", False),
    ("ts9", "TIMESTAMP(9)", False),
    ("cl", "CLOB", False),
    ("bl", "BLOB", False),
]
"""Круг Oracle -> Oracle: типы источника как есть; VARCHAR2(20 CHAR) на
однобайтовой базе становится VARCHAR2(20)."""

EXPECTED_POSTGRES = [
    ("id", "bigint", True),
    ("n19", "numeric(19,0)", False),
    ("n18_4", "numeric(18,4)", False),
    ("nfree", "numeric(38,0)", False),
    ("f126", "double precision", False),
    ("bd", "double precision", False),
    ("bf", "real", False),
    ("vc", "character varying(40)", False),
    ("vcc", "character varying(20)", False),
    ("nvc", "character varying(20)", False),
    ("c5", "character varying(5)", False),
    ("r16", "character varying(32)", False),
    ("d", "timestamp(0) without time zone", False),
    ("ts6", "timestamp(6) without time zone", False),
    ("ts9", "timestamp(6) without time zone", False),
    ("cl", "text", False),
]

EXPECTED_CLICKHOUSE = {
    "id": "Int64",
    "n19": "Nullable(Decimal(19, 0))",
    "n18_4": "Nullable(Decimal(18, 4))",
    "nfree": "Nullable(Decimal(38, 0))",
    "f126": "Nullable(Float64)",
    "bd": "Nullable(Float64)",
    "bf": "Nullable(Float32)",
    "vc": "Nullable(String)",
    "c5": "Nullable(String)",
    "r16": "Nullable(String)",
    "d": "Nullable(DateTime64(0))",
    "ts6": "Nullable(DateTime64(6))",
    "ts9": "Nullable(DateTime64(9))",
    "cl": "Nullable(String)",
    "bl": "Nullable(String)",
}

COMPARED_ORACLE = {
    "vc": "vc",
    "vcc": "vcc",
    "c5": "c5",
    "n18_4": "to_char(n18_4)",
    "nfree": "to_char(nfree)",
    "f126": "to_char(cast(f126 as binary_double))",
    "bd": "to_char(bd)",
    "r16": "rawtohex(r16)",
    "d": "to_char(d, 'yyyy-mm-dd hh24:mi:ss')",
    "ts6": "to_char(ts6, 'yyyy-mm-dd hh24:mi:ss.ff6')",
    "ts9": "to_char(ts9, 'yyyy-mm-dd hh24:mi:ss.ff6')",
    "cl": "dbms_lob.getlength(cl)",
    "bl": "rawtohex(dbms_lob.substr(bl, 3, 1))",
}
"""Опорные выражения круга: значение текстом на обеих таблицах Oracle;
FLOAT(126) драйвер читает как double, TIMESTAMP(9) — до микросекунд:
сравниваются double и микросекунды."""


def _newest_postgres() -> Any:
    newest: Any = None
    for source in STAND.sources:
        if source.name.startswith("pg-"):
            newest = source

    if newest is None:
        raise AssertionError("no postgres source on the stand")

    return newest


def _newest_clickhouse() -> Any:
    return STAND.demo_clickhouse()[-1]


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
async def oracle(request: Any) -> AsyncIterator[OracleSide]:
    side = OracleSide(request.param, ARRAYSIZE)
    await side.connect()
    await side.recreate_user()
    await side.create(SRC, TYPED_DDL)
    await side.run((TYPED_FILL,))

    yield side

    await side.drop()


@pytest.fixture(scope="module")
async def postgres() -> AsyncIterator[PostgresSide]:
    side = PostgresSide(_newest_postgres(), PG_SCHEMA)
    await side.connect()
    await side.recreate_schema()

    yield side

    await side.drop()


@pytest.fixture(scope="module")
async def clickhouse() -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(_newest_clickhouse(), CH_DATABASE)
    await side.connect()
    await side.recreate_database()

    yield side

    await side.drop()


def ora_out(select: str, columns: Sequence[ColumnDeclaration] = DECLARED) -> Leg:
    return Leg("ora_stream_out", {"sql": select, "columns": columns})


def ora_in(  # noqa: PLR0913
    table: str,
    schema: Any,
    delete: Any = DeleteNothing(kind="nothing"),
    rules: ColumnRules = ColumnRules(),
    unknown_types: Any = FailOnUnknown(kind="fail_on_unknown"),
    insert: Any = InsertFull(kind="full"),
    create_table: str = OraTableRef.CREATE_TABLE,
    after: Sequence[str] = (),
) -> Leg:
    return Leg(
        "ora_stream_in",
        {
            "schema_name": OWNER,
            "table_name": table,
            "schema_strategy": schema,
            "delete_strategy": delete,
            "insert_strategy": insert,
            "rules": rules,
            "unknown_types": unknown_types,
            "create_table": create_table,
            "chunk_bytes": CHUNK,
            "after": after,
        },
    )


async def circle(oracle: OracleSide, out: Leg, into: Leg) -> str:
    pumps = Pumps(oracle=oracle.profile)
    chained = await pumps.chain(out, into)
    print(f"\n--- {oracle.source.name}: ora_stream_out ---\n{chained.out_report}")
    print(f"--- ora_stream_in ---\n{chained.in_report}")

    return chained.in_report


def expected_oracle(oracle: OracleSide) -> list[tuple[str, str, bool]]:
    columns: list[tuple[str, str, bool]] = []
    for name, kind, not_null in EXPECTED_ORACLE:
        text = kind
        if name == "vcc" and not oracle.unicode:
            text = "VARCHAR2(20)"

        columns.append((name, text, not_null))

    return columns


async def same_values(oracle: OracleSide, table: str) -> None:
    source = OraLoaded(oracle, SRC)
    landed = OraLoaded(oracle, table)
    for name, _, _ in EXPECTED_ORACLE:
        expression = COMPARED_ORACLE.get(name, name)
        expected = await source.column(expression)
        actual = await landed.column(expression)

        assert actual == expected, f"column {name} differs"


class TestOracleCircle:
    async def test_types_survive_the_circle(self, oracle: OracleSide) -> None:
        report = await circle(
            oracle,
            ora_out(SELECT),
            ora_in("circle", DropAndCreate(kind="drop_and_create")),
        )
        landed = OraLoaded(oracle, "circle")

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == expected_oracle(oracle)
        assert await landed.count() == ROWS
        assert await landed.scalar("count(vcc)") == await OraLoaded(oracle, SRC).scalar(
            "count(vcc)"
        )
        await same_values(oracle, "circle")

    async def test_reload_passes_the_check(self, oracle: OracleSide) -> None:
        report = await circle(
            oracle,
            ora_out(SELECT),
            ora_in(
                "circle",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                DeleteTruncate(kind="truncate"),
            ),
        )

        assert f"{ROWS} rows loaded" in report
        assert "error" not in report.split("rows loaded")[0].lower()
        assert await OraLoaded(oracle, "circle").count() == ROWS


class TestOracleToOthers:
    async def test_postgres_creates_the_table(
        self, oracle: OracleSide, postgres: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=postgres.profile)
        table = f"from_{oracle.source.name}".replace("-", "_").replace(".", "_")
        chained = await pumps.chain(
            ora_out(SELECT_TEXT_ONLY),
            Leg(
                "pg_stream_in",
                {
                    "schema_name": PG_SCHEMA,
                    "table_name": table,
                    "schema_strategy": DropAndCreate(kind="drop_and_create"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "copy_options": CopyOptions(chunk_bytes=CHUNK),
                },
            ),
        )
        landed = Loaded(postgres, PG_SCHEMA, table)
        source = OraLoaded(oracle, SRC)

        assert f"{ROWS} rows loaded" in chained.in_report
        assert await landed.columns() == EXPECTED_POSTGRES
        assert await landed.count() == ROWS
        assert await landed.aggregate("sum(n19)") == Decimal(
            str(await source.scalar("sum(n19)"))
        )
        assert await landed.aggregate("sum(n18_4)") == Decimal(
            str(await source.scalar("sum(n18_4)"))
        )
        assert await landed.scalars(
            "to_char(ts6, 'YYYY-MM-DD HH24:MI:SS.US')"
        ) == await source.column("to_char(ts6, 'yyyy-mm-dd hh24:mi:ss.ff6')")
        assert await landed.texts("vc") == await source.column("vc")
        assert await landed.texts("r16") == await source.column("rawtohex(r16)")

    async def test_clickhouse_creates_the_table(
        self, oracle: OracleSide, clickhouse: ClickHouseSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, clickhouse=clickhouse.profile)
        table = f"from_{oracle.source.name}".replace("-", "_").replace(".", "_")
        select = SELECT.replace(', nvc as "nvc"', "").replace(', vcc as "vcc"', "")
        chained = await pumps.chain(
            ora_out(select),
            Leg(
                "ch_stream_in",
                {
                    "database": CH_DATABASE,
                    "table_name": table,
                    "schema_strategy": DropAndCreate(kind="drop_and_create"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "create_table": MERGE_TREE,
                    "order_by": "id",
                },
            ),
        )
        landed = ChLoaded(clickhouse, table)
        source = OraLoaded(oracle, SRC)

        assert f"{ROWS} rows loaded" in chained.in_report
        assert dict(await landed.types()) == EXPECTED_CLICKHOUSE
        assert await landed.count() == ROWS
        assert await landed.scalar("sum(n18_4)") == await source.scalar("sum(n18_4)")
        assert await landed.column("vc") == await source.column("vc")


PG_DDL = f"""
create table {PG_SCHEMA}.src (
    id bigint not null,
    i2 smallint,
    n numeric(12,2),
    f8 double precision,
    b boolean,
    t text,
    vc varchar(30),
    bin bytea,
    d date,
    ts timestamp(6),
    tz timestamptz(3),
    tm time,
    u uuid,
    j jsonb,
    ip inet,
    iv interval,
    arr integer[]
)"""
PG_FILL = f"""
insert into {PG_SCHEMA}.src
select g, (g % 30000)::smallint, g * 1.25, g / 7.0, g % 2 = 0,
       'text ' || g || E' \\t tab', 'v' || g, decode(lpad(to_hex(g), 6, '0'), 'hex'),
       date '2024-01-01' + g % 300,
       timestamp '2024-02-29 13:14:15.123456' + (g || ' seconds')::interval,
       timestamptz '2024-02-29 13:14:15.123+00' + (g || ' seconds')::interval,
       time '13:14:15.123456' + (g || ' seconds')::interval,
       md5(g::text)::uuid, jsonb_build_object('g', g), ('10.0.0.' || g % 250)::inet,
       (g || ' minutes')::interval, array[g, g + 1]
from generate_series(1, {ROWS}) g"""

EXPECTED_FROM_POSTGRES = [
    ("id", "NUMBER(19,0)", True),
    ("i2", "NUMBER(5,0)", False),
    ("n", "NUMBER(12,2)", False),
    ("f8", "BINARY_DOUBLE", False),
    ("b", "{boolean}", False),
    ("t", "CLOB", False),
    ("vc", "VARCHAR2(30 CHAR)", False),
    ("bin", "CLOB", False),
    ("d", "DATE", False),
    ("ts", "TIMESTAMP(6)", False),
    ("tz", "TIMESTAMP(3) WITH TIME ZONE", False),
    ("tm", "VARCHAR2(18 CHAR)", False),
    ("u", "VARCHAR2(36 CHAR)", False),
    ("j", "CLOB", False),
    ("ip", "CLOB", False),
    ("iv", "CLOB", False),
]
"""Таблица Oracle из потока postgres без массива: bytea postgres присылает
hex-текстом, и он ложится строкой как есть; boolean до 23 источник шлёт
числом (b::int), это integer — NUMBER(10)."""


def expected_from_postgres(oracle: OracleSide) -> list[tuple[str, str, bool]]:
    boolean = "BOOLEAN"
    if oracle.version < BOOLEAN_SINCE:
        boolean = "NUMBER(10,0)"

    columns: list[tuple[str, str, bool]] = []
    for name, kind, not_null in EXPECTED_FROM_POSTGRES:
        columns.append((name, kind.format(boolean=boolean), not_null))

    return columns


def pg_out(select: str) -> Leg:
    return Leg(
        "pg_stream_out",
        {
            "sql": select,
            "wire": StreamWire.ARROW,
            "columns": (PgColumnDeclaration(name="id", nullable=False),),
            "copy_options": CopyOptions(chunk_bytes=CHUNK),
        },
    )


@pytest.fixture(scope="module")
async def pg_source(postgres: PostgresSide) -> PostgresSide:
    await postgres.execute([PG_DDL, PG_FILL])

    return postgres


class TestPostgresIntoOracle:
    SELECT: ClassVar[str] = (
        f"select id, i2, n, f8, {{b}}, t, vc, bin, d, ts, tz, tm, u, j, ip, iv "
        f"from {PG_SCHEMA}.src"
    )

    def select(self, oracle: OracleSide) -> str:
        """bool до 23 драйвер в Oracle не кладёт: LLM шлёт его числом."""
        if oracle.version < BOOLEAN_SINCE:
            return self.SELECT.format(b="b::int as b")

        return self.SELECT.format(b="b")

    async def test_raw_bool_is_refused_before_23(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        if oracle.version >= BOOLEAN_SINCE:
            pytest.skip("the server has BOOLEAN")

        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        with pytest.raises(
            TransferError, match=r"column b: bool .*send an integer 0 or 1"
        ):
            await pumps.chain(
                pg_out(f"select id, b from {PG_SCHEMA}.src"),
                ora_in("from_pg", DropAndCreate(kind="drop_and_create")),
            )

    async def test_array_is_refused(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        with pytest.raises(TransferError, match=r"column arr: .*integer\[\]"):
            await pumps.chain(
                pg_out(f"select id, arr from {PG_SCHEMA}.src"),
                ora_in("from_pg", DropAndCreate(kind="drop_and_create")),
            )

    async def test_array_lands_as_clob_on_fallback(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        chained = await pumps.chain(
            pg_out(f"select id, arr from {PG_SCHEMA}.src"),
            ora_in(
                "from_pg",
                DropAndCreate(kind="drop_and_create"),
                unknown_types=FallbackAsVarchar(kind="fallback_as_varchar"),
            ),
        )
        landed = OraLoaded(oracle, "from_pg")

        assert f"{ROWS} rows loaded" in chained.in_report
        assert await landed.columns() == [
            ("id", "NUMBER(19,0)", True),
            ("arr", "CLOB", False),
        ]
        assert (await landed.column("to_char(arr)"))[:2] == ["{1,2}", "{2,3}"]

    async def test_every_family_lands_with_oracle_types(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        chained = await pumps.chain(
            pg_out(self.select(oracle)),
            ora_in("from_pg", DropAndCreate(kind="drop_and_create")),
        )
        landed = OraLoaded(oracle, "from_pg")
        source = Loaded(pg_source, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in chained.in_report
        assert await landed.columns() == expected_from_postgres(oracle)
        assert await landed.count() == ROWS
        assert await landed.scalar("sum(n)") == await source.aggregate("sum(n)")
        assert await landed.column("u") == await source.texts("u")
        assert await landed.column("tm") == await source.texts("tm")
        assert await landed.column("to_char(d, 'yyyy-mm-dd')") == (
            await source.texts("d")
        )
        assert await landed.column(
            "to_char(ts, 'yyyy-mm-dd hh24:mi:ss.ff6')"
        ) == await source.scalars("to_char(ts, 'YYYY-MM-DD HH24:MI:SS.US')")
        assert await landed.column("to_char(cast(j as varchar2(100)))") == (
            await source.texts("j")
        )
        assert await landed.column("to_char(bin)") == await source.texts("bin")
        truthy_expression = "count(case when b = true then 1 end)"
        if oracle.version < BOOLEAN_SINCE:
            truthy_expression = "sum(b)"

        truthy = await landed.scalar(truthy_expression)

        assert truthy == await source.aggregate("count(*) filter (where b)")

    async def test_reload_passes_the_check(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        chained = await pumps.chain(
            pg_out(self.select(oracle)),
            ora_in(
                "from_pg",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                DeleteTruncate(kind="truncate"),
            ),
        )

        assert f"{ROWS} rows loaded" in chained.in_report
        assert "error" not in chained.in_report.split("rows loaded")[0].lower()

    async def test_zoned_stream_into_unzoned_table_is_refused(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        await pumps.chain(
            pg_out(f"select id, ts from {PG_SCHEMA}.src"),
            ora_in("moments", DropAndCreate(kind="drop_and_create")),
        )
        with pytest.raises(TransferError, match="ts: time zone differs"):
            await pumps.chain(
                pg_out(f"select id, tz as ts from {PG_SCHEMA}.src"),
                ora_in(
                    "moments",
                    ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                ),
            )


class TestStrategies:
    """Стратегии приёмника на потоке postgres -> Oracle."""

    MONTH: ClassVar[str] = "2024-03"

    async def test_delete_where_replaces_a_slice(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        select = f"select id, d, vc from {PG_SCHEMA}.src"
        await pumps.chain(
            pg_out(select), ora_in("slices", DropAndCreate(kind="drop_and_create"))
        )
        landed = OraLoaded(oracle, "slices")
        await oracle.run(
            (
                f"update {OWNER}.slices set vc = 'stale' "
                f"where to_char(d, 'yyyy-mm') = '{self.MONTH}'",
            )
        )
        in_month = await Loaded(pg_source, PG_SCHEMA, "src").aggregate(
            f"count(*) filter (where to_char(d, 'YYYY-MM') = '{self.MONTH}')"
        )

        chained = await pumps.chain(
            pg_out(f"{select} where to_char(d, 'YYYY-MM') = '{self.MONTH}'"),
            ora_in(
                "slices",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                DeleteWhere(
                    kind="delete_where",
                    where=f"to_char(d, 'yyyy-mm') = '{self.MONTH}'",
                ),
            ),
        )

        assert in_month > 0
        assert f"{in_month} rows deleted" in chained.in_report
        assert f"{in_month} rows loaded" in chained.in_report
        assert await landed.scalar("count(case when vc = 'stale' then 1 end)") == 0
        assert await landed.count() == ROWS

    async def test_renamed_mart_with_declared_types(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        chained = await pumps.chain(
            pg_out(f"select id, n, vc, ts from {PG_SCHEMA}.src"),
            ora_in(
                "mart",
                DropAndCreate(kind="drop_and_create"),
                rules=ColumnRules(
                    rename_columns={"key": "id", "amount": "n", "label": "vc"},
                    column_types={"amount": "NUMBER(14,3)", "label": "VARCHAR2(10)"},
                ),
            ),
        )
        landed = OraLoaded(oracle, "mart")

        assert f"{ROWS} rows loaded" in chained.in_report
        assert await landed.columns() == [
            ("key", "NUMBER(19,0)", True),
            ("amount", "NUMBER(14,3)", False),
            ("label", "VARCHAR2(10)", False),
            ("ts", "TIMESTAMP(6)", False),
        ]
        assert await landed.column("label", order_by="key") == await Loaded(
            pg_source, PG_SCHEMA, "src"
        ).texts("vc")

    async def test_narrower_table_is_refused_before_loading(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        await oracle.create(
            "narrow", ["id number(19) not null", "n number(8,1)", "vc varchar2(3)"]
        )
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        try:
            with pytest.raises(
                TransferError, match=r"n: table NUMBER\(8,1\) truncates"
            ):
                await pumps.chain(
                    pg_out(f"select id, n, vc from {PG_SCHEMA}.src"),
                    ora_in(
                        "narrow", ErrorIfSchemaChanged(kind="error_if_schema_changed")
                    ),
                )

            assert await OraLoaded(oracle, "narrow").count() == 0
        finally:
            await oracle.drop_table("narrow")

    async def test_drift_is_refused_then_backed_up(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        select = f"select id, n, vc from {PG_SCHEMA}.src"
        await pumps.chain(
            pg_out(select), ora_in("drift", DropAndCreate(kind="drop_and_create"))
        )
        await oracle.run((f"alter table {OWNER}.drift drop column vc",))

        with pytest.raises(
            TransferError, match="vc: in the stream but not in the table"
        ):
            await pumps.chain(
                pg_out(select),
                ora_in("drift", ErrorIfSchemaChanged(kind="error_if_schema_changed")),
            )

        chained = await pumps.chain(
            pg_out(select),
            ora_in(
                "drift",
                BackupAndCreateIfSchemaChanged(
                    kind="backup_and_create_if_schema_changed"
                ),
            ),
        )
        drift = OraLoaded(oracle, "drift")
        backups: list[str] = []
        for name in await drift.tables():
            if name.startswith("drift_bak_"):
                backups.append(name)

        assert "saved as" in chained.in_report
        assert "drift_bak_" in chained.in_report
        assert len(backups) == 1
        assert await OraLoaded(oracle, backups[0]).count() == ROWS
        assert await drift.count() == ROWS
        assert ("vc", "VARCHAR2(30 CHAR)", False) in await drift.columns()

    async def test_create_template_and_after_steps(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        chained = await pumps.chain(
            pg_out(f"select id, n from {PG_SCHEMA}.src"),
            ora_in(
                "templated",
                DropAndCreate(kind="drop_and_create"),
                create_table=(
                    "create table {schema_name}.{table_name} ({columns}) "
                    "tablespace users nologging"
                ),
                after=(f"update {OWNER}.templated set n = n * 2",),
            ),
        )
        landed = OraLoaded(oracle, "templated")

        assert f"{ROWS} rows loaded" in chained.in_report
        assert f"statement: update {OWNER}.templated set n = n * 2" in chained.in_report
        assert await landed.scalar("sum(n)") == 2 * await Loaded(
            pg_source, PG_SCHEMA, "src"
        ).aggregate("sum(n)")
        assert (
            await landed.scalar(
                f"(select logging from all_tables where owner = '{OWNER}' "
                "and table_name = 'TEMPLATED')"
            )
            == "NO"
        )

    async def test_dry_run_changes_nothing(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        landed = OraLoaded(oracle, "templated")
        columns = await landed.columns()
        chained = await pumps.chain(
            pg_out(f"select id, n from {PG_SCHEMA}.src"),
            ora_in(
                "templated",
                DoNothing(kind="do_nothing"),
                insert=InsertNothing(kind="nothing"),
            ),
        )

        assert chained.in_report.startswith("0 rows loaded")
        assert await landed.columns() == columns
        assert await landed.count() == ROWS

    async def test_failed_after_step_rolls_back_the_rows(
        self, oracle: OracleSide, pg_source: PostgresSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, postgres=pg_source.profile)
        landed = OraLoaded(oracle, "templated")
        with pytest.raises(OracleQueryError, match="ORA-00942"):
            await pumps.chain(
                pg_out(f"select id, n from {PG_SCHEMA}.src"),
                ora_in(
                    "templated",
                    CreateIfNotExists(kind="create_if_not_exists"),
                    DeleteTruncate(kind="truncate"),
                    after=("insert into no_such_table values (1)",),
                ),
            )

        # truncate — DDL с автокоммитом, а вставка откатилась вместе с ошибкой шага
        assert await landed.count() == 0


CH_DDL = f"""
create table {CH_DATABASE}.src (
    id Int64,
    u8 UInt8,
    dec Decimal(18, 4),
    f32 Float32,
    s String,
    lc LowCardinality(String),
    d Date,
    dt DateTime64(3, 'UTC'),
    b Bool,
    u UUID
) engine = MergeTree order by id"""
CH_FILL = f"""
insert into {CH_DATABASE}.src
select number, number % 200, toDecimal64(number, 4) / 7, number / 3,
       concat('s', toString(number)), concat('lc', toString(number % 3)),
       toDate('2024-01-01') + number % 300,
       toDateTime64('2024-02-29 13:14:15.123', 3, 'UTC') + number,
       number % 2 = 0, generateUUIDv4()
from numbers(1, {ROWS})"""

EXPECTED_FROM_CLICKHOUSE = [
    ("id", "NUMBER(19,0)", True),
    ("u8", "NUMBER(3,0)", True),
    ("dec", "NUMBER(18,4)", True),
    ("f32", "BINARY_FLOAT", True),
    ("s", "CLOB", False),
    ("lc", "CLOB", False),
    ("d", "DATE", True),
    ("dt", "TIMESTAMP(3) WITH TIME ZONE", True),
    ("b", "{boolean}", True),
    ("u", "CLOB", False),
]
"""Таблица Oracle из потока ClickHouse: uuid LLM шлёт текстом (toString),
bool до 23 — числом (toUInt8, это UInt8 — NUMBER(3))."""


@pytest.fixture(scope="module")
async def ch_source(clickhouse: ClickHouseSide) -> ClickHouseSide:
    await clickhouse.command(CH_DDL)
    await clickhouse.command(CH_FILL)

    return clickhouse


def ch_out(select: str) -> Leg:
    return Leg(
        "ch_stream_out",
        {
            "sql": f"{select} settings output_format_arrow_string_as_string = 1",
            "wire": ChStreamWire.ARROW,
            "chunk_bytes": CHUNK,
        },
    )


class TestClickHouseIntoOracle:
    SELECT: ClassVar[str] = (
        f"select id, u8, dec, f32, s, lc, d, dt, {{b}}, toString(u) as u "
        f"from {CH_DATABASE}.src order by id"
    )

    def select(self, oracle: OracleSide) -> str:
        if oracle.version < BOOLEAN_SINCE:
            return self.SELECT.format(b="toUInt8(b) as b")

        return self.SELECT.format(b="b")

    async def test_raw_uuid_is_refused(
        self, oracle: OracleSide, ch_source: ClickHouseSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, clickhouse=ch_source.profile)
        with pytest.raises(
            TransferError, match=r"column u: .*uuid extension: send utf8 text"
        ):
            await pumps.chain(
                ch_out(f"select id, u from {CH_DATABASE}.src order by id"),
                ora_in("from_ch", DropAndCreate(kind="drop_and_create")),
            )

    async def test_every_family_lands_with_oracle_types(
        self, oracle: OracleSide, ch_source: ClickHouseSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile, clickhouse=ch_source.profile)
        chained = await pumps.chain(
            ch_out(self.select(oracle)),
            ora_in("from_ch", DropAndCreate(kind="drop_and_create")),
        )
        landed = OraLoaded(oracle, "from_ch")
        source = ChLoaded(ch_source, "src")
        boolean = "BOOLEAN"
        if oracle.version < BOOLEAN_SINCE:
            boolean = "NUMBER(3,0)"

        expected: list[tuple[str, str, bool]] = []
        for name, kind, not_null in EXPECTED_FROM_CLICKHOUSE:
            expected.append((name, kind.format(boolean=boolean), not_null))

        assert f"{ROWS} rows loaded" in chained.in_report
        assert await landed.columns() == expected
        assert await landed.count() == ROWS
        assert await landed.scalar("sum(dec)") == await source.scalar("sum(dec)")
        assert await landed.column("to_char(u)") == [
            str(value) for value in await source.column("u")
        ]
        assert await landed.column("to_char(s)") == await source.column("s")
        assert await landed.column(
            "to_char(sys_extract_utc(dt), 'yyyy-mm-dd hh24:mi:ss.ff3')"
        ) == await source.column("toString(dt)")


class TestSourceRefusals:
    """Типы, которые драйвер в Arrow отдаёт с потерей или не отдаёт: отказ
    до выполнения запроса с подсказкой."""

    async def test_zoned_timestamp_needs_a_conversion(self, oracle: OracleSide) -> None:
        pumps = Pumps(oracle=oracle.profile)
        select = (
            f'select id as "id", from_tz(ts6, \'+03:00\') as "tstz" from {OWNER}.{SRC}'
        )
        with pytest.raises(OracleQueryError, match=r"tstz.*sys_extract_utc"):
            await pumps.chain(
                ora_out(select),
                ora_in("refused", DropAndCreate(kind="drop_and_create")),
            )

        assert "refused" not in await OraLoaded(oracle, SRC).tables()

    async def test_utc_wall_time_lands_as_zoned_column(
        self, oracle: OracleSide
    ) -> None:
        pumps = Pumps(oracle=oracle.profile)
        select = (
            f'select id as "id", sys_extract_utc(from_tz(ts6, \'+03:00\')) as "utc" '
            f"from {OWNER}.{SRC}"
        )
        chained = await pumps.chain(
            ora_out(select),
            ora_in(
                "utc",
                DropAndCreate(kind="drop_and_create"),
                rules=ColumnRules(column_types={"utc": "TIMESTAMP(6) WITH TIME ZONE"}),
            ),
        )
        landed = OraLoaded(oracle, "utc")

        assert f"{ROWS} rows loaded" in chained.in_report
        assert ("utc", "TIMESTAMP(6) WITH TIME ZONE", False) in await landed.columns()
        assert (
            await landed.column("to_char(utc, 'yyyy-mm-dd hh24:mi:ss.ff6 tzh:tzm')")
        )[0] == "2024-02-29 10:14:16.123456 +00:00"

    @pytest.mark.parametrize(
        ("expression", "hint"),
        [
            ("numtodsinterval(id, 'second')", "INTERVAL_DS"),
            ("numtoyminterval(id, 'month')", "INTERVAL_YM"),
            ("xmltype('<a/>')", "xmlserialize"),
        ],
    )
    async def test_unfetchable_types_are_refused(
        self, oracle: OracleSide, expression: str, hint: str
    ) -> None:
        pumps = Pumps(oracle=oracle.profile)
        select = f'select id as "id", {expression} as "v" from {OWNER}.{SRC}'
        with pytest.raises(OracleQueryError, match=hint):
            await pumps.chain(
                ora_out(select),
                ora_in("refused", DropAndCreate(kind="drop_and_create")),
            )

    async def test_declaration_on_a_missing_column(self, oracle: OracleSide) -> None:
        pumps = Pumps(oracle=oracle.profile)
        with pytest.raises(ContractError, match="no column 'nope'"):
            await pumps.chain(
                ora_out(SELECT, (ColumnDeclaration(name="nope", nullable=False),)),
                ora_in("refused", DropAndCreate(kind="drop_and_create")),
            )
