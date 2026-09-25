"""Стейтменты before и after у насосов: на каждой базе стенда они идут в той
же сессии, что и команда насоса, поэтому temp-таблица из before видна
загрузке и after, а разбор staging и подмена партиции делаются одним
вызовом. Проверяется и обратная сторона: что ошибка шага after откатывает
загрузку там, где база это умеет (PostgreSQL, Oracle), и оставляет строки
там, где транзакций нет (ClickHouse)."""

# ruff: noqa: S608 — стейтменты стенда собираются текстом, как их пишет LLM

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

import psycopg
import pytest

from boba.db.clickhouse import ClickHouseQueryError
from boba.db.oracle import OracleQueryError
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

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
CHUNK_BYTES = 4096

TSV = b"1\tnew1\n3\tnew3\n"
CSV = b"1,new1\n3,new3\n"
OLD_ROWS = ((1, "old1"), (2, "old2"))
UPSERTED = [(1, "new1"), (2, "old2"), (3, "new3")]


class PgScripts:
    """Схема PostgreSQL: target с двумя строками, mirror пустая."""

    SCHEMA: ClassVar[str] = "pump_scripts"

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
        """COPY во временную таблицу из before, delete и insert в after — одна
        транзакция, target получил upsert; в отчёте статус каждого шага."""
        pumps = Pumps(postgres=pg.side.profile)

        report = await pumps.pg_in(
            "copy stage_tmp from stdin",
            TSV,
            before=["create temp table stage_tmp (id bigint, v text) on commit drop"],
            after=[
                f"delete from {pg.named('target')} t using stage_tmp s "
                "where t.id = s.id",
                f"insert into {pg.named('target')} select id, v from stage_tmp",
            ],
        )

        assert await pg.rows("target") == UPSERTED
        assert "before:\n- CREATE TABLE: create temp table" in report
        assert "- DELETE 1: delete from" in report
        assert "- INSERT 0 2: insert into" in report

    async def test_failing_after_rolls_back_the_load(self, pg: PgScripts) -> None:
        """Ошибка последнего шага after откатывает и COPY, и предыдущие шаги."""
        pumps = Pumps(postgres=pg.side.profile)

        with pytest.raises(psycopg.Error, match="stop"):
            await pumps.pg_in(
                f"copy {pg.named('mirror')} from stdin",
                TSV,
                after=[
                    f"insert into {pg.named('mirror')} values (9, 'nine')",
                    "do $$ begin raise exception 'stop'; end $$",
                ],
            )

        assert await pg.rows("mirror") == []

    async def test_out_reads_temp_table_from_before(self, pg: PgScripts) -> None:
        """Выгрузка видит temp-таблицу, созданную в before той же сессии."""
        pumps = Pumps(postgres=pg.side.profile)

        exported = await pumps.pg_out(
            "copy snap to stdout",
            before=[
                "create temp table snap as "
                f"select id, v from {pg.named('target')} where id = 2"
            ],
        )

        assert exported == b"2\told2\n"

    async def test_arrow_chain_with_scripts(self, pg: PgScripts) -> None:
        """Arrow-насосы принимают те же скрипты: приёмник грузит поток во
        временную таблицу и переносит его в mirror шагом after."""
        pumps = Pumps(postgres=pg.side.profile)

        chained = await pumps.chain(
            Leg(
                "pg_arrow_out",
                {
                    "sql": f"select id, v from {pg.named('target')} order by id",
                    "chunk_bytes": CHUNK_BYTES,
                },
            ),
            Leg(
                "pg_arrow_in",
                {
                    "sql": "copy stage_tmp (id, v) from stdin (format csv)",
                    "chunk_bytes": CHUNK_BYTES,
                    "before": [
                        "create temp table stage_tmp (id bigint, v text) on commit drop"
                    ],
                    "after": [
                        f"insert into {pg.named('mirror')} select id, v from stage_tmp"
                    ],
                },
            ),
        )

        assert await pg.rows("mirror") == list(OLD_ROWS)
        assert "- INSERT 0 2: insert into" in chained.in_report


class ChScripts:
    """База ClickHouse: target и партиционированная part_target со старыми
    строками, stage той же раскладки, что part_target."""

    DATABASE: ClassVar[str] = "pump_scripts"

    def __init__(self, source: ChSource) -> None:
        self.side = ClickHouseSide(source, self.DATABASE)

    async def recreate(self) -> None:
        await self.side.connect()
        await self.side.recreate_database()
        await self.side.create("target", ("id UInt64", "v String"))
        await self.side.command(
            f"insert into {self.named('target')} values (1, 'old1'), (2, 'old2')"
        )
        for table in ("part_target", "stage"):
            await self.side.command(
                f"create table {self.named(table)} (d Date, id UInt64, v String) "
                "engine = MergeTree partition by toYYYYMM(d) order by id"
            )

        await self.side.command(
            f"insert into {self.named('part_target')} values "
            "('2024-08-01', 1, 'aug'), ('2024-09-01', 2, 'old2')"
        )

    async def rows(self, table: str, *expressions: str) -> list[Any]:
        listed: list[Any] = []
        for row in await self.side.select(table, expressions):
            listed.append(tuple(row))

        return listed

    def named(self, table: str) -> str:
        return f"{self.DATABASE}.{table}"


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
    async def test_temp_table_lives_in_the_pump_session(self, ch: ChScripts) -> None:
        """SET и временная таблица из before доживают до INSERT и after: у
        насоса одна сессия сервера на весь вызов."""
        pumps = Pumps(clickhouse=ch.side.profile)

        report = await pumps.ch_in(
            "insert into stage_tmp format TabSeparated",
            TSV,
            before=[
                "set max_insert_block_size = 1000",
                "create temporary table stage_tmp (id UInt64, v String)",
            ],
            after=[
                "select count() from stage_tmp",
                f"insert into {ch.named('target')} select id, upper(v) from stage_tmp",
            ],
        )

        assert await ch.rows("target", "id", "v") == [
            (1, "NEW1"),
            (1, "old1"),
            (2, "old2"),
            (3, "NEW3"),
        ]
        assert "- 2: select count() from stage_tmp" in report
        assert "- read 2 rows, written 2 rows: insert into" in report

    async def test_replace_partition_from_stage(self, ch: ChScripts) -> None:
        """Загрузка в stage и replace partition в after: месяц подменён целиком,
        соседняя партиция не тронута."""
        pumps = Pumps(clickhouse=ch.side.profile)

        await pumps.ch_in(
            f"insert into {ch.named('stage')} format TabSeparated",
            b"2024-09-02\t5\tsep5\n2024-09-03\t6\tsep6\n",
            before=[f"truncate table {ch.named('stage')}"],
            after=[
                f"alter table {ch.named('part_target')} replace partition 202409 "
                f"from {ch.named('stage')}"
            ],
        )

        assert await ch.rows("part_target", "id", "v") == [
            (1, "aug"),
            (5, "sep5"),
            (6, "sep6"),
        ]

    async def test_failing_after_keeps_loaded_rows(self, ch: ChScripts) -> None:
        """Транзакций нет: ошибка шага after не откатывает INSERT."""
        pumps = Pumps(clickhouse=ch.side.profile)

        with pytest.raises(ClickHouseQueryError, match="stop"):
            await pumps.ch_in(
                f"insert into {ch.named('target')} format TabSeparated",
                TSV,
                after=["select throwIf(1, 'stop')"],
            )

        assert len(await ch.rows("target", "id")) == 4


class OraScripts:
    """Схема PUMP_STAND на модуль: target с двумя строками, mirror пустая,
    глобальная временная stage_tmp; партиционированная part_target и
    stage_part под exchange partition создаёт сам тест. Тесты не делят
    таблицы между собой."""

    def __init__(self, source: OraSource) -> None:
        self.side = OracleSide(source, arraysize=100)

    async def recreate(self) -> None:
        await self.side.connect()
        await self.side.recreate_user()
        await self.side.create(
            "target", ("id number(10) primary key", "v varchar2(50)")
        )
        await self.side.create("mirror", ("id number(10)", "v varchar2(50)"))
        await self.side.run(
            (
                "create global temporary table stage_tmp (id number(10), "
                "v varchar2(50)) on commit delete rows",
                "insert into target values (1, 'old1')",
                "insert into target values (2, 'old2')",
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
                "insert into part_target values (1, 202408, 'aug')",
                "insert into part_target values (2, 202409, 'old2')",
            )
        )

    async def rows(self, table: str, *expressions: str) -> list[Any]:
        listed: list[Any] = []
        for row in await self.side.select(table, expressions):
            listed.append(tuple(row))

        return listed


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

        report = await pumps.ora_in(
            "insert into stage_tmp (id, v) values (:1, :2)",
            CSV,
            before=["delete from stage_tmp"],
            after=[
                "delete from target where id in (select id from stage_tmp)",
                "insert into target select id, v from stage_tmp",
            ],
        )

        assert await ora.rows("target", "id", "v") == UPSERTED
        assert "before:\n- 0 rows: delete from stage_tmp" in report
        assert "- 1 rows: delete from target" in report
        assert "- 2 rows: insert into target" in report

    async def test_failing_after_rolls_back_the_load(self, ora: OraScripts) -> None:
        """Блок PL/SQL с raise_application_error в after срывает вызов до
        commit: ни загрузка, ни предыдущий шаг after не остались."""
        pumps = Pumps(oracle=ora.side.profile)

        with pytest.raises(OracleQueryError, match="stop"):
            await pumps.ora_in(
                "insert into mirror (id, v) values (:1, :2)",
                CSV,
                after=[
                    "insert into mirror values (9, 'nine')",
                    "begin raise_application_error(-20001, 'stop'); end;",
                ],
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

        report = await pumps.ora_in(
            "insert into stage_part (id, m, v) values (:1, :2, :3)",
            b"5,202409,sep5\n6,202409,sep6\n",
            before=["truncate table stage_part"],
            after=[
                "alter table part_target exchange partition p_202409 "
                "with table stage_part"
            ],
        )

        assert await ora.rows("part_target", "id", "v") == [
            (1, "aug"),
            (5, "sep5"),
            (6, "sep6"),
        ]
        assert "after:\n- 0 rows: alter table part_target exchange partition" in report
