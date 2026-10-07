# ruff: noqa: S608, E501
"""Стейтменты before и after у источников и приёмников sync: на каждой базе
стенда они идут в той же сессии, что и команда насоса, поэтому временная
таблица из before видна запросу источника, загрузке и after, а разбор
staging и подмена партиции делаются одним вызовом. Проверяется и обратная
сторона: что ошибка шага after откатывает загрузку там, где база это умеет
(PostgreSQL, Oracle), и оставляет строки там, где транзакций нет
(ClickHouse).

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from boba.db.oracle import OracleQueryError
from boba.pump_stand import (
    ChSource,
    ClickHouseSide,
    OracleSide,
    OraSource,
    PgSource,
    PostgresSide,
    PumpDags,
    Pumps,
    PumpStand,
)
from boba.pump_stand.oracle import PumpUser
from boba.stand.names import StandNames
from boba.stand_core.progress import HeardProgress
from boba.toolkit.result import SqlFailureResult
from boba.toolrun.dag_run import DagOutcome

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()

OLD_ROWS = ((1, "old1"), (2, "old2"))
UPSERTED = [(1, "new1"), (2, "old2"), (3, "new3")]

PG_UPSERT = """
name = "scripts_pg_upsert"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, v from $pg_schema.stage_src order by id"
wire = "csv"
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "stage_tmp"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
copy_options = { chunk_bytes = 4096 }
after = [
    "delete from $pg_schema.target t using $pg_schema.stage_tmp s where t.id = s.id",
    "insert into $pg_schema.target select id, v from $pg_schema.stage_tmp",
    "drop table $pg_schema.stage_tmp",
]
feed = "rows"
"""

PG_FAILING_AFTER = """
name = "scripts_pg_failing_after"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, v from $pg_schema.target"
wire = "csv"
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "mirror"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
copy_options = { chunk_bytes = 4096 }
after = [
    "insert into $pg_schema.mirror values (9, 'nine')",
    "do $$$$ begin raise exception 'stop'; end $$$$",
]
feed = "rows"
"""
"""Блок do $$ ... $$: в string.Template литеральный $ пишется как $$."""

PG_ARROW_SCRIPTS = """
name = "scripts_pg_arrow"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, v from $pg_schema.target order by id"
wire = "arrow"
copy_options = { chunk_bytes = 4096 }
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "stage_arrow"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
copy_options = { chunk_bytes = 4096 }
after = ["insert into $pg_schema.mirror select id, v from $pg_schema.stage_arrow"]
feed = "rows"
"""

CH_RECEIVER_SESSION = """
name = "scripts_ch_receiver_session"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, v from $ch_database.fresh order by id"
wire = "tsv"
chunk_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "staged"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
before = [
    "set max_insert_block_size = 1000",
    "create temporary table seen (n UInt64)",
]
after = [
    "insert into seen select count() from $ch_database.staged",
    "select n from seen",
    "insert into $ch_database.target select id, upper(v) from $ch_database.staged",
]
feed = "rows"
"""

CH_SOURCE_SESSION = """
name = "scripts_ch_source_session"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, v from snap"
wire = "tsv"
chunk_bytes = 4096
before = [
    "create temporary table snap (id UInt64, v String)",
    "insert into snap select id, v from $ch_database.target where id = 2",
]
after = ["select count() from snap"]
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "mirror"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
feed = "rows"
"""

CH_REPLACE_PARTITION = """
name = "scripts_ch_replace_partition"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select d, id, v from $ch_database.fresh_part order by id"
wire = "tsv"
chunk_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "stage"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
before = ["truncate table $ch_database.stage"]
after = ["alter table $ch_database.part_target replace partition 202409 from $ch_database.stage"]
feed = "rows"
"""

CH_FAILING_AFTER = """
name = "scripts_ch_failing_after"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, v from $ch_database.fresh"
wire = "tsv"
chunk_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "target"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
after = ["select throwIf(1, 'stop')"]
feed = "rows"
"""

ORA_UPSERT = """
name = "scripts_ora_upsert"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = "select id, v from fresh order by id"
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "stage_tmp"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
chunk_bytes = 4096
before = ["delete from stage_tmp"]
after = [
    "delete from target where id in (select id from stage_tmp)",
    "insert into target select id, v from stage_tmp",
]
feed = "rows"
"""

ORA_SOURCE_SESSION = """
name = "scripts_ora_source_session"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = "select id, v from snap_tmp"
before = ["insert into snap_tmp select id, v from target where id = 2"]
after = ["delete from snap_tmp"]
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "snapshot"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
chunk_bytes = 4096
feed = "rows"
"""

ORA_FAILING_AFTER = """
name = "scripts_ora_failing_after"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = "select id, v from fresh"
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "mirror"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
chunk_bytes = 4096
after = [
    "insert into mirror values (9, 'nine')",
    "begin raise_application_error(-20001, 'stop'); end;",
]
feed = "rows"
"""

ORA_EXCHANGE_PARTITION = """
name = "scripts_ora_exchange_partition"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = "select id, m, v from fresh_part order by id"
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "stage_part"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
chunk_bytes = 4096
before = ["truncate table stage_part"]
after = ["alter table part_target exchange partition p_202409 with table stage_part"]
feed = "rows"
"""


async def _run(dags: PumpDags, spec: str) -> DagOutcome:
    """Запуск описания с печатью отчётов обоих узлов; сбой — ошибка теста."""
    outcome = await dags.run(spec)
    for node in outcome.nodes:
        print(f"\n--- {outcome.dag}: {node.key} ---\n{node.content}")

    failed: list[str] = []
    for node in outcome.failures():
        failed.append(f"{node.key}: {node.content}")

    assert outcome.ok(), "\n".join(failed)

    return outcome


def _sql_failure(outcome: DagOutcome, key: str) -> SqlFailureResult:
    """Сбой узла key как SqlFailureResult: ошибка сервера и команды до неё."""
    failure = outcome.failure(key)
    print(f"\n--- {outcome.dag}: {key} failed ---\n{failure.llm_view()}")

    assert isinstance(failure, SqlFailureResult), failure

    return failure


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


@pytest.fixture
def pg_dags(tmp_path: Path, pg: PgScripts) -> PumpDags:
    return PumpDags(tmp_path, {"pg": pg.side.profile}, {"pg_schema": pg.SCHEMA})


class TestPostgres:
    async def test_upsert_through_temp_table(
        self, pg: PgScripts, pg_dags: PumpDags
    ) -> None:
        """Поток во временную таблицу из before, delete и insert в after — одна
        транзакция, target получил upsert; в отчёте статус каждого шага."""
        await pg.side.create("stage_src", ("id bigint", "v text"))
        await pg.side.execute(
            ("insert into stage_src values (1, 'new1'), (3, 'new3')",)
        )

        outcome = await _run(pg_dags, PG_UPSERT)
        in_report = outcome.node("dst").content

        assert await pg.rows("target") == UPSERTED
        assert "DELETE 1\nstatement: delete from" in in_report
        assert "INSERT 0 2\nstatement: insert into" in in_report

    async def test_failing_after_rolls_back_the_load(
        self, pg: PgScripts, pg_dags: PumpDags
    ) -> None:
        """Ошибка последнего шага after откатывает и загрузку, и предыдущие шаги."""
        outcome = await pg_dags.run(PG_FAILING_AFTER)

        failure = _sql_failure(outcome, "dst")
        assert "stop" in failure.llm_view()
        assert failure.statements[-1].status == "failed: RaiseException"
        assert await pg.rows("mirror") == []

    async def _out_messages(self, pg: PgScripts, statement: str) -> list[str]:
        """Отчёты выгрузки statement о ходе работы, по порядку."""
        heard = HeardProgress()
        try:
            pumps = Pumps(postgres=pg.side.profile, progress=heard.progress)
            await pumps.pg_out(statement)

            return heard.messages()
        finally:
            heard.close()

    async def test_out_reports_its_settings_and_the_connection(
        self, pg: PgScripts
    ) -> None:
        """Выгрузка сообщает, с какими настройками идёт (и взятыми по
        умолчанию), и о подключении до и после него."""
        messages = await self._out_messages(
            pg, f"select id, v from {pg.named('target')}"
        )

        settings = messages[0]
        if not settings.startswith("postgres pump settings: "):
            raise AssertionError(f"the settings come first: {messages}")
        if f"chunk_bytes={Pumps.CHUNK_BYTES}" not in settings:
            raise AssertionError(f"the chunk size in use is named: {settings}")
        if "exact_floats=False" not in settings or "datestyle=" not in settings:
            raise AssertionError(f"defaults are named too: {settings}")

        if messages[1] != "connecting to postgres, waiting for the server":
            raise AssertionError(f"the wait for the connection: {messages[1]}")
        if not messages[2].startswith("connected to postgres in "):
            raise AssertionError(f"the connection is confirmed: {messages[2]}")
        if "server " not in messages[2] or "backend pid " not in messages[2]:
            raise AssertionError(f"the server introduces itself: {messages[2]}")

    async def test_out_reports_the_command_whole_and_its_outcome(
        self, pg: PgScripts
    ) -> None:
        """Команда сервера названа целиком одной строкой до отправки, итог —
        со статусом сервера; адреса и входа в отчётах нет."""
        messages = await self._out_messages(
            pg, f"select id,\n       v\n  from {pg.named('target')}"
        )

        flat = f"copy ( select id, v from {pg.named('target')}"
        sent = [text for text in messages if text.startswith("running on postgres")]
        if not sent or flat not in sent[-1]:
            raise AssertionError(f"the command is named whole on one line: {sent}")

        done = [text for text in messages if text.startswith("postgres answered in ")]
        if not done or ": COPY " not in done[-1]:
            raise AssertionError(f"the outcome of the command is named: {messages}")

        profile = pg.side.profile
        for text in messages:
            if str(profile.host) in text or profile.trace() in text:
                raise AssertionError(f"no address or login in a report: {text}")

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

    async def test_arrow_chain_with_scripts(
        self, pg: PgScripts, pg_dags: PumpDags
    ) -> None:
        """Провод arrow принимает те же скрипты: приёмник грузит поток во
        временную таблицу и переносит его в mirror шагом after."""
        outcome = await _run(pg_dags, PG_ARROW_SCRIPTS)
        in_report = outcome.node("dst").content

        assert await pg.rows("mirror") == list(OLD_ROWS)
        assert "INSERT 0 2\nstatement: insert into" in in_report


class ChScripts:
    """База ClickHouse: target со старыми строками, fresh с новыми, staged под
    загрузку и mirror под снимок; партиционированные part_target со старыми
    строками, stage той же раскладки и fresh_part с новым месяцем."""

    DATABASE: ClassVar[str] = StandNames().of("pump_scripts")

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


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
def ch_source(request: Any) -> ChSource:
    return request.param


@pytest.fixture
async def ch(ch_source: ChSource) -> AsyncIterator[ChScripts]:
    made = ChScripts(ch_source)
    await made.recreate()
    yield made
    await made.side.drop()


@pytest.fixture
def ch_dags(tmp_path: Path, ch: ChScripts) -> PumpDags:
    return PumpDags(tmp_path, {"ch": ch.side.profile}, {"ch_database": ch.DATABASE})


class TestClickHouse:
    async def test_temp_table_lives_in_the_receiver_session(
        self, ch: ChScripts, ch_dags: PumpDags
    ) -> None:
        """SET и временная таблица из before доживают до загрузки и after: у
        приёмника одна сессия сервера на весь вызов."""
        outcome = await _run(ch_dags, CH_RECEIVER_SESSION)
        in_report = outcome.node("dst").content

        assert await ch.rows("target", "id", "v") == [
            (1, "NEW1"),
            (1, "old1"),
            (2, "old2"),
            (3, "NEW3"),
        ]
        assert "2\nstatement: select n from seen" in in_report
        assert "read 2 rows, written 2 rows\nstatement: insert into" in in_report

    async def test_source_reads_temp_table_from_before(
        self, ch: ChScripts, ch_dags: PumpDags
    ) -> None:
        """Запрос источника видит временную таблицу из своего before, after
        идёт в той же сессии после выгрузки."""
        outcome = await _run(ch_dags, CH_SOURCE_SESSION)
        out_report = outcome.node("src").content

        assert await ch.rows("mirror", "id", "v") == [(2, "old2")]
        assert "statement: create temporary table snap" in out_report
        assert "1\nstatement: select count() from snap" in out_report

    async def test_replace_partition_from_stage(
        self, ch: ChScripts, ch_dags: PumpDags
    ) -> None:
        """Загрузка в stage и replace partition в after: месяц подменён целиком,
        соседняя партиция не тронута."""
        await _run(ch_dags, CH_REPLACE_PARTITION)

        assert await ch.rows("part_target", "id", "v") == [
            (1, "aug"),
            (5, "sep5"),
            (6, "sep6"),
        ]

    async def test_failing_after_keeps_loaded_rows(
        self, ch: ChScripts, ch_dags: PumpDags
    ) -> None:
        """Транзакций нет: ошибка шага after не откатывает загрузку."""
        outcome = await ch_dags.run(CH_FAILING_AFTER)

        failure = _sql_failure(outcome, "dst")
        assert "stop" in failure.llm_view()
        assert failure.statements[-1].status == "failed: ClickHouseQueryError"
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


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
def ora_source(request: Any) -> OraSource:
    return request.param


@pytest.fixture(scope="module")
async def ora(ora_source: OraSource) -> AsyncIterator[OraScripts]:
    made = OraScripts(ora_source)
    await made.recreate()
    yield made
    await made.side.drop()


@pytest.fixture
def ora_dags(tmp_path: Path, ora: OraScripts) -> PumpDags:
    return PumpDags(
        tmp_path, {"ora": ora.side.profile}, {"ora_schema": PumpUser().name}
    )


class TestOracle:
    async def test_upsert_through_temporary_table(
        self, ora: OraScripts, ora_dags: PumpDags
    ) -> None:
        """Загрузка в глобальную временную таблицу, delete и insert в after,
        один commit: target получил upsert, в отчёте строки каждого шага."""
        outcome = await _run(ora_dags, ORA_UPSERT)
        in_report = outcome.node("dst").content

        assert await ora.rows("target", "id", "v") == UPSERTED
        assert "0 rows\nstatement: delete from stage_tmp" in in_report
        assert "1 rows\nstatement: delete from target" in in_report
        assert "2 rows\nstatement: insert into target" in in_report

    async def test_source_reads_temporary_table_from_before(
        self, ora: OraScripts, ora_dags: PumpDags
    ) -> None:
        """Строки, вставленные в глобальную временную таблицу в before, видны
        запросу источника только в его сессии; after идёт там же до commit."""
        outcome = await _run(ora_dags, ORA_SOURCE_SESSION)
        out_report = outcome.node("src").content

        assert await ora.rows("snapshot", "id", "v") == [(2, "old2")]
        assert "1 rows\nstatement: insert into snap_tmp" in out_report
        assert "1 rows\nstatement: delete from snap_tmp" in out_report

    async def test_failing_after_rolls_back_the_load(
        self, ora: OraScripts, ora_dags: PumpDags
    ) -> None:
        """Блок PL/SQL с raise_application_error в after срывает вызов до
        commit: ни загрузка, ни предыдущий шаг after не остались."""
        outcome = await ora_dags.run(ORA_FAILING_AFTER)

        failure = _sql_failure(outcome, "dst")
        assert "stop" in failure.llm_view()
        assert failure.statements[-1].status == "failed: OracleQueryError"
        assert await ora.rows("mirror", "id", "v") == []

    async def test_exchange_partition_from_stage(
        self, ora: OraScripts, ora_dags: PumpDags
    ) -> None:
        """Загрузка в stage_part и exchange partition в after: партиция месяца
        подменена целиком, соседняя не тронута. Без опции Partitioning
        (ORA-00439) тест пропускается."""
        try:
            await ora.recreate_partitioned()
        except OracleQueryError as exc:
            if "ORA-00439" not in str(exc):
                raise

            pytest.skip(f"partitioning is not available: {exc}")

        outcome = await _run(ora_dags, ORA_EXCHANGE_PARTITION)
        in_report = outcome.node("dst").content

        assert await ora.rows("part_target", "id", "v") == [
            (1, "aug"),
            (5, "sep5"),
            (6, "sep6"),
        ]
        assert (
            "0 rows\nstatement: alter table part_target exchange partition" in in_report
        )
