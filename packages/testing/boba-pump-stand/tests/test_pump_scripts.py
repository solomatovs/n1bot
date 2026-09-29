"""Стейтменты before и after у источников и приёмников sync: на каждой базе
стенда они идут в той же сессии, что и команда насоса, поэтому временная
таблица из before видна запросу источника, загрузке и after, а разбор
staging и подмена партиции делаются одним вызовом. Проверяется и обратная
сторона: что ошибка шага after откатывает загрузку там, где база это умеет
(PostgreSQL, Oracle), и оставляет строки там, где транзакций нет
(ClickHouse)."""

# ruff: noqa: S608 — стейтменты стенда собираются текстом, как их пишет LLM

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

import psycopg
import pytest

from boba.db.clickhouse import ClickHouseQueryError
from boba.db.clickhouse.target import ChStreamWire
from boba.db.oracle import OracleQueryError
from boba.db.postgres.connection import CopyOptions
from boba.pump_stand import (
    ChSource,
    ClickHouseSide,
    Leg,
    OracleSide,
    OraSource,
    PgSource,
    PostgresSide,
    Pumps,
    PumpStand,
)
from boba.stand.names import StandNames
from boba.pump_stand.oracle import PumpUser
from boba.toolkit.transfer import (
    CreateIfNotExists,
    DeleteNothing,
    ErrorIfNotExists,
    InsertFull,
    StreamWire,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
CHUNK_BYTES = 4096

OLD_ROWS = ((1, "old1"), (2, "old2"))
UPSERTED = [(1, "new1"), (2, "old2"), (3, "new3")]


class PgScripts:
    """Схема PostgreSQL: target с двумя строками, mirror пустая."""

    SCHEMA: ClassVar[str] = StandNames().of("pump_scripts")

    def __init__(self, source: PgSource) -> None:
        self.side = PostgresSide(source, self.SCHEMA)

    async def recreate(self) -> None:
        await self.side.connect()
        await self.side.recreate_schema()
        await self.side.create("target", ("id bigint primary key", "v text"))
        await self.side.create("mirror", ("id bigint", "v text"))
        await self.side.execute(("insert into target values (1, 'old1'), (2, 'old2')",))

    async def rows(self, table: str) -> list[Any]:
        listed: list[Any] = []
        for row in await self.side.select(table, ("id", "v")):
            listed.append(tuple(row))

        return listed

    def named(self, table: str) -> str:
        return f"{self.SCHEMA}.{table}"


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
def pg_source(request: Any) -> PgSource:
    return request.param


@pytest.fixture
async def pg(pg_source: PgSource) -> AsyncIterator[PgScripts]:
    made = PgScripts(pg_source)
    await made.recreate()
    yield made
    await made.side.drop()


class TestPostgres:
    async def test_upsert_through_temp_table(self, pg: PgScripts) -> None:
        """Поток во временную таблицу из before, delete и insert в after — одна
        транзакция, target получил upsert; в отчёте статус каждого шага."""
        pumps = Pumps(postgres=pg.side.profile)
        await pg.side.create("stage_src", ("id bigint", "v text"))
        await pg.side.execute(
            ("insert into stage_src values (1, 'new1'), (3, 'new3')",)
        )

        chained = await pumps.chain(
            Leg(
                "pg_stream_out",
                {
                    "sql": f"select id, v from {pg.named('stage_src')} order by id",
                    "wire": StreamWire.CSV,
                    "copy_options": CopyOptions(chunk_bytes=CHUNK_BYTES),
                },
            ),
            Leg(
                "pg_stream_in",
                {
                    "schema_name": pg.SCHEMA,
                    "table_name": "stage_tmp",
                    "schema_strategy": CreateIfNotExists(kind="create_if_not_exists"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "copy_options": CopyOptions(chunk_bytes=CHUNK_BYTES),
                    "after": [
                        f"delete from {pg.named('target')} t using "
                        f"{pg.named('stage_tmp')} s where t.id = s.id",
                        f"insert into {pg.named('target')} select id, v from "
                        f"{pg.named('stage_tmp')}",
                        f"drop table {pg.named('stage_tmp')}",
                    ],
                },
            ),
        )

        assert await pg.rows("target") == UPSERTED
        assert "after:\n- DELETE 1: delete from" in chained.in_report
        assert "INSERT 0 2\nstatement: insert into" in chained.in_report

    async def test_failing_after_rolls_back_the_load(self, pg: PgScripts) -> None:
        """Ошибка последнего шага after откатывает и загрузку, и предыдущие шаги."""
        pumps = Pumps(postgres=pg.side.profile)

        with pytest.raises(psycopg.Error, match="stop"):
            await pumps.chain(
                Leg(
                    "pg_stream_out",
                    {
                        "sql": f"select id, v from {pg.named('target')}",
                        "wire": StreamWire.CSV,
                        "copy_options": CopyOptions(chunk_bytes=CHUNK_BYTES),
                    },
                ),
                Leg(
                    "pg_stream_in",
                    {
                        "schema_name": pg.SCHEMA,
                        "table_name": "mirror",
                        "schema_strategy": ErrorIfNotExists(kind="error_if_not_exists"),
                        "delete_strategy": DeleteNothing(kind="nothing"),
                        "insert_strategy": InsertFull(kind="full"),
                        "copy_options": CopyOptions(chunk_bytes=CHUNK_BYTES),
                        "after": [
                            f"insert into {pg.named('mirror')} values (9, 'nine')",
                            "do $$ begin raise exception 'stop'; end $$",
                        ],
                    },
                ),
            )

        assert await pg.rows("mirror") == []

    async def test_out_reads_temp_table_from_before(self, pg: PgScripts) -> None:
        """Выгрузка видит temp-таблицу, созданную в before той же сессии."""
        pumps = Pumps(postgres=pg.side.profile)

        exported = await pumps.pg_out(
            "select id, v from snap",
            before=[
                "create temp table snap as "
                f"select id, v from {pg.named('target')} where id = 2"
            ],
        )

        assert exported == b"2\told2\n"

    async def test_arrow_chain_with_scripts(self, pg: PgScripts) -> None:
        """Провод arrow принимает те же скрипты: приёмник грузит поток во
        временную таблицу и переносит его в mirror шагом after."""
        pumps = Pumps(postgres=pg.side.profile)

        chained = await pumps.chain(
            Leg(
                "pg_stream_out",
                {
                    "sql": f"select id, v from {pg.named('target')} order by id",
                    "wire": StreamWire.ARROW,
                    "copy_options": CopyOptions(chunk_bytes=CHUNK_BYTES),
                },
            ),
            Leg(
                "pg_stream_in",
                {
                    "schema_name": pg.SCHEMA,
                    "table_name": "stage_arrow",
                    "schema_strategy": CreateIfNotExists(kind="create_if_not_exists"),
                    "delete_strategy": DeleteNothing(kind="nothing"),
                    "insert_strategy": InsertFull(kind="full"),
                    "copy_options": CopyOptions(chunk_bytes=CHUNK_BYTES),
                    "after": [
                        f"insert into {pg.named('mirror')} select id, v from "
                        f"{pg.named('stage_arrow')}"
                    ],
                },
            ),
        )

        assert await pg.rows("mirror") == list(OLD_ROWS)
        assert "INSERT 0 2\nstatement: insert into" in chained.in_report


class ChScripts:
    """База ClickHouse: target со старыми строками, fresh с новыми, staged под
    загрузку и mirror под снимок; партиционированные part_target со старыми
    строками, stage той же раскладки и fresh_part с новым месяцем."""

    DATABASE: ClassVar[str] = StandNames().of("pump_scripts")
    MERGE_TREE: ClassVar[str] = (
        "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) "
        "engine = MergeTree order by {order_by}"
    )
    """Шаблон для серверов без Keeper: дефолт ch_stream_in — ReplicatedMergeTree."""

    def __init__(self, source: ChSource) -> None:
        self.side = ClickHouseSide(source, self.DATABASE)

    async def recreate(self) -> None:
        await self.side.connect()
        await self.side.recreate_database()
        for table in ("target", "fresh", "staged"):
            await self.side.create(table, ("id UInt64", "v String"))

        await self.side.command(
            f"insert into {self.named('target')} values (1, 'old1'), (2, 'old2')"
        )
        await self.side.command(
            f"insert into {self.named('fresh')} values (1, 'new1'), (3, 'new3')"
        )
        for table in ("part_target", "stage", "fresh_part"):
            await self.side.command(
                f"create table {self.named(table)} (d Date, id UInt64, v String) "
                "engine = MergeTree partition by toYYYYMM(d) order by id"
            )

        await self.side.command(
            f"insert into {self.named('part_target')} values "
            "('2024-08-01', 1, 'aug'), ('2024-09-01', 2, 'old2')"
        )
        await self.side.command(
            f"insert into {self.named('fresh_part')} values "
            "('2024-09-02', 5, 'sep5'), ('2024-09-03', 6, 'sep6')"
        )

    async def rows(self, table: str, *expressions: str) -> list[Any]:
        listed: list[Any] = []
        for row in await self.side.select(table, expressions):
            listed.append(tuple(row))

        return listed

    def named(self, table: str) -> str:
        return f"{self.DATABASE}.{table}"

    def out(self, sql: str, **extra: Any) -> Leg:
        arguments: dict[str, Any] = {
            "sql": sql,
            "wire": ChStreamWire.TSV,
            "chunk_bytes": CHUNK_BYTES,
        }
        arguments.update(extra)

        return Leg("ch_stream_out", arguments)

    def into(self, table: str, **extra: Any) -> Leg:
        arguments: dict[str, Any] = {
            "database": self.DATABASE,
            "table_name": table,
            "schema_strategy": ErrorIfNotExists(kind="error_if_not_exists"),
            "delete_strategy": DeleteNothing(kind="nothing"),
            "insert_strategy": InsertFull(kind="full"),
            "create_table": self.MERGE_TREE,
        }
        arguments.update(extra)

        return Leg("ch_stream_in", arguments)


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
def ch_source(request: Any) -> ChSource:
    return request.param


@pytest.fixture
async def ch(ch_source: ChSource) -> AsyncIterator[ChScripts]:
    made = ChScripts(ch_source)
    await made.recreate()
    yield made
    await made.side.drop()


class TestClickHouse:
    async def test_temp_table_lives_in_the_receiver_session(
        self, ch: ChScripts
    ) -> None:
        """SET и временная таблица из before доживают до загрузки и after: у
        приёмника одна сессия сервера на весь вызов."""
        pumps = Pumps(clickhouse=ch.side.profile)

        chained = await pumps.chain(
            ch.out(f"select id, v from {ch.named('fresh')} order by id"),
            ch.into(
                "staged",
                before=[
                    "set max_insert_block_size = 1000",
                    "create temporary table seen (n UInt64)",
                ],
                after=[
                    f"insert into seen select count() from {ch.named('staged')}",
                    "select n from seen",
                    f"insert into {ch.named('target')} select id, upper(v) from "
                    f"{ch.named('staged')}",
                ],
            ),
        )

        assert await ch.rows("target", "id", "v") == [
            (1, "NEW1"),
            (1, "old1"),
            (2, "old2"),
            (3, "NEW3"),
        ]
        assert "2\nstatement: select n from seen" in chained.in_report
        assert (
            "read 2 rows, written 2 rows\nstatement: insert into" in chained.in_report
        )

    async def test_source_reads_temp_table_from_before(self, ch: ChScripts) -> None:
        """Запрос источника видит временную таблицу из своего before, after
        идёт в той же сессии после выгрузки."""
        pumps = Pumps(clickhouse=ch.side.profile)

        chained = await pumps.chain(
            ch.out(
                "select id, v from snap",
                before=[
                    "create temporary table snap (id UInt64, v String)",
                    f"insert into snap select id, v from {ch.named('target')} "
                    "where id = 2",
                ],
                after=["select count() from snap"],
            ),
            ch.into(
                "mirror", schema_strategy=CreateIfNotExists(kind="create_if_not_exists")
            ),
        )

        assert await ch.rows("mirror", "id", "v") == [(2, "old2")]
        assert "before:\n- " in chained.out_report
        assert "after:\n- 1: select count() from snap" in chained.out_report

    async def test_replace_partition_from_stage(self, ch: ChScripts) -> None:
        """Загрузка в stage и replace partition в after: месяц подменён целиком,
        соседняя партиция не тронута."""
        pumps = Pumps(clickhouse=ch.side.profile)

        await pumps.chain(
            ch.out(f"select d, id, v from {ch.named('fresh_part')} order by id"),
            ch.into(
                "stage",
                before=[f"truncate table {ch.named('stage')}"],
                after=[
                    f"alter table {ch.named('part_target')} replace partition "
                    f"202409 from {ch.named('stage')}"
                ],
            ),
        )

        assert await ch.rows("part_target", "id", "v") == [
            (1, "aug"),
            (5, "sep5"),
            (6, "sep6"),
        ]

    async def test_failing_after_keeps_loaded_rows(self, ch: ChScripts) -> None:
        """Транзакций нет: ошибка шага after не откатывает загрузку."""
        pumps = Pumps(clickhouse=ch.side.profile)

        with pytest.raises(ClickHouseQueryError, match="stop"):
            await pumps.chain(
                ch.out(f"select id, v from {ch.named('fresh')}"),
                ch.into("target", after=["select throwIf(1, 'stop')"]),
            )

        assert len(await ch.rows("target", "id")) == 4


class OraScripts:
    """Схема PUMP_STAND на модуль: target с двумя строками, fresh с новыми,
    пустые mirror и snapshot, глобальные временные stage_tmp и snap_tmp;
    партиционированную part_target, stage_part и fresh_part под exchange
    partition создаёт сам тест. Тесты не делят таблицы между собой."""

    def __init__(self, source: OraSource) -> None:
        self.side = OracleSide(source, arraysize=100)

    async def recreate(self) -> None:
        await self.side.connect()
        await self.side.recreate_user()
        await self.side.create(
            "target", ("id number(10) primary key", "v varchar2(50)")
        )
        for table in ("fresh", "mirror", "snapshot"):
            await self.side.create(table, ("id number(10)", "v varchar2(50)"))

        await self.side.run(
            (
                "create global temporary table stage_tmp (id number(10), "
                "v varchar2(50)) on commit delete rows",
                "create global temporary table snap_tmp (id number(10), "
                "v varchar2(50)) on commit delete rows",
                "insert into target values (1, 'old1')",
                "insert into target values (2, 'old2')",
                "insert into fresh values (1, 'new1')",
                "insert into fresh values (3, 'new3')",
            )
        )

    async def recreate_partitioned(self) -> None:
        await self.side.run(
            (
                "create table part_target (id number(10), m number(6), "
                "v varchar2(50)) partition by list (m) "
                "(partition p_202408 values (202408), "
                "partition p_202409 values (202409))",
                "create table stage_part (id number(10), m number(6), v varchar2(50))",
                "create table fresh_part (id number(10), m number(6), v varchar2(50))",
                "insert into part_target values (1, 202408, 'aug')",
                "insert into part_target values (2, 202409, 'old2')",
                "insert into fresh_part values (5, 202409, 'sep5')",
                "insert into fresh_part values (6, 202409, 'sep6')",
            )
        )

    async def rows(self, table: str, *expressions: str) -> list[Any]:
        listed: list[Any] = []
        for row in await self.side.select(table, expressions):
            listed.append(tuple(row))

        return listed

    def out(self, sql: str, **extra: Any) -> Leg:
        arguments: dict[str, Any] = {"sql": sql}
        arguments.update(extra)

        return Leg("ora_stream_out", arguments)

    def into(self, table: str, **extra: Any) -> Leg:
        arguments: dict[str, Any] = {
            "schema_name": PumpUser().name,
            "table_name": table,
            "schema_strategy": ErrorIfNotExists(kind="error_if_not_exists"),
            "delete_strategy": DeleteNothing(kind="nothing"),
            "insert_strategy": InsertFull(kind="full"),
            "chunk_bytes": CHUNK_BYTES,
        }
        arguments.update(extra)

        return Leg("ora_stream_in", arguments)


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
def ora_source(request: Any) -> OraSource:
    return request.param


@pytest.fixture(scope="module")
async def ora(ora_source: OraSource) -> AsyncIterator[OraScripts]:
    made = OraScripts(ora_source)
    await made.recreate()
    yield made
    await made.side.drop()


class TestOracle:
    async def test_upsert_through_temporary_table(self, ora: OraScripts) -> None:
        """Загрузка в глобальную временную таблицу, delete и insert в after,
        один commit: target получил upsert, в отчёте строки каждого шага."""
        pumps = Pumps(oracle=ora.side.profile)

        chained = await pumps.chain(
            ora.out("select id, v from fresh order by id"),
            ora.into(
                "stage_tmp",
                before=["delete from stage_tmp"],
                after=[
                    "delete from target where id in (select id from stage_tmp)",
                    "insert into target select id, v from stage_tmp",
                ],
            ),
        )

        assert await ora.rows("target", "id", "v") == UPSERTED
        assert "before:\n- 0 rows: delete from stage_tmp" in chained.in_report
        assert "1 rows\nstatement: delete from target" in chained.in_report
        assert "2 rows\nstatement: insert into target" in chained.in_report

    async def test_source_reads_temporary_table_from_before(
        self, ora: OraScripts
    ) -> None:
        """Строки, вставленные в глобальную временную таблицу в before, видны
        запросу источника только в его сессии; after идёт там же до commit."""
        pumps = Pumps(oracle=ora.side.profile)

        chained = await pumps.chain(
            ora.out(
                "select id, v from snap_tmp",
                before=["insert into snap_tmp select id, v from target where id = 2"],
                after=["delete from snap_tmp"],
            ),
            ora.into("snapshot"),
        )

        assert await ora.rows("snapshot", "id", "v") == [(2, "old2")]
        assert "before:\n- 1 rows: insert into snap_tmp" in chained.out_report
        assert "after:\n- 1 rows: delete from snap_tmp" in chained.out_report

    async def test_failing_after_rolls_back_the_load(self, ora: OraScripts) -> None:
        """Блок PL/SQL с raise_application_error в after срывает вызов до
        commit: ни загрузка, ни предыдущий шаг after не остались."""
        pumps = Pumps(oracle=ora.side.profile)

        with pytest.raises(OracleQueryError, match="stop"):
            await pumps.chain(
                ora.out("select id, v from fresh"),
                ora.into(
                    "mirror",
                    after=[
                        "insert into mirror values (9, 'nine')",
                        "begin raise_application_error(-20001, 'stop'); end;",
                    ],
                ),
            )

        assert await ora.rows("mirror", "id", "v") == []

    async def test_exchange_partition_from_stage(self, ora: OraScripts) -> None:
        """Загрузка в stage_part и exchange partition в after: партиция месяца
        подменена целиком, соседняя не тронута. Без опции Partitioning
        (ORA-00439) тест пропускается."""
        try:
            await ora.recreate_partitioned()
        except OracleQueryError as exc:
            if "ORA-00439" not in str(exc):
                raise

            pytest.skip(f"partitioning is not available: {exc}")

        pumps = Pumps(oracle=ora.side.profile)

        chained = await pumps.chain(
            ora.out("select id, m, v from fresh_part order by id"),
            ora.into(
                "stage_part",
                before=["truncate table stage_part"],
                after=[
                    "alter table part_target exchange partition p_202409 "
                    "with table stage_part"
                ],
            ),
        )

        assert await ora.rows("part_target", "id", "v") == [
            (1, "aug"),
            (5, "sep5"),
            (6, "sep6"),
        ]
        assert (
            "after:\n- 0 rows: alter table part_target exchange partition"
            in chained.in_report
        )
