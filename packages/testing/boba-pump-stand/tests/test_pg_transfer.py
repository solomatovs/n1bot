# ruff: noqa: S608, PLR0913
"""Загрузка postgres -> postgres парой boba-stream-pg-to-pg через pg_stream_out и
pg_stream_in с раскладками csv, tsv и binary: контракт RowDescription как есть, сверка
по OID и typmod с каталогом приёмника, DDL текстом типа источника, тела COPY
без перекодирования. Прогон по всем postgres стенда и Greenplum.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Any, ClassVar

import psycopg
import pytest

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import CopyOptions
from boba.db.postgres.query import PgQueryBuilder
from boba.db.postgres.transfer import (
    PgCatalogColumn,
    PgColumnDeclaration,
    PgCopyLayout,
    PgCopyOut,
    PgTransferTable,
    PgTypeRules,
)
from boba.pump_stand import Leg, Loaded, PostgresSide, Pumps, PumpStand
from boba.pump_stand.names import StandNames
from boba.pump_stand.ports import Sink, SinkOutbound
from boba.stream.pg_to_pg.transfer import PgStreamColumn
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
    FailOnUnknown,
    FallbackAsVarchar,
    InsertFull,
    InsertNothing,
    StreamWire,
    TransferError,
    TransferOutbound,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
S = StandNames().of("pump_transfer")
CHUNK = 4096
ROWS = 60
SELECT = f"select * from {S}.src"


def _newest(sources: Sequence[Any]) -> str:
    plain: list[str] = []
    for source in sources:
        if source.name.startswith("pg-"):
            plain.append(source.name)

    return plain[-1]


NEWEST = _newest(STAND.sources)


def _older_than(version: int) -> Any:
    """Самый новый postgres стенда ниже данной версии сервера (по имени
    pg-<major>), None — такого нет."""
    chosen: Any = None
    for source in STAND.sources:
        if not source.name.startswith("pg-"):
            continue

        major = source.name.removeprefix("pg-")
        if float(major) * 10000 < version:
            chosen = source

    return chosen


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, S)
    await side.connect()
    await side.recreate_schema(
        [
            "create type mood as enum ('sad', 'happy')",
            "create type mood2 as enum ('sad', 'happy')",
            f"""
            create table {S}.src (
                id bigint not null,
                name varchar(50) not null,
                amount numeric(18,4),
                dt timestamp(3),
                flag boolean,
                note text
            )""",
            f"""
            insert into {S}.src
            select g, 'name ' || g, g / 7.0,
                   timestamp '2024-01-01' + g * interval '1 min',
                   g % 2 = 0, case when g % 5 = 0 then null else 'note ' || g end
            from generate_series(1, {ROWS}) g""",
        ]
    )

    yield side

    await side.drop()


def only_newest(postgres: PostgresSide) -> None:
    if postgres.source.name != NEWEST:
        pytest.skip("one postgres is enough here")


async def land(
    postgres: PostgresSide,
    table: str,
    schema: Any = CreateIfNotExists(kind="create_if_not_exists"),
    delete: Any = DeleteNothing(kind="nothing"),
    insert: Any = InsertFull(kind="full"),
    rules: ColumnRules = ColumnRules(),
    unknown_types: Any = FailOnUnknown(kind="fail_on_unknown"),
    select: str = SELECT,
    columns: Sequence[PgColumnDeclaration] = (),
    wire: StreamWire = StreamWire.CSV,
    create_table: str = PgTransferTable.CREATE_TABLE,
) -> str:
    """Источник -> приёмник на одном сервере, id объявлен not null."""
    pumps = Pumps(postgres=postgres.profile)
    declared = [PgColumnDeclaration(name="id", nullable=False), *columns]
    chained = await pumps.chain(
        Leg(
            "pg_stream_out",
            {
                "sql": select,
                "wire": wire,
                "columns": declared,
                "copy_options": CopyOptions(chunk_bytes=CHUNK),
            },
        ),
        Leg(
            "pg_stream_in",
            {
                "schema_name": S,
                "table_name": table,
                "schema_strategy": schema,
                "delete_strategy": delete,
                "insert_strategy": insert,
                "rules": rules,
                "unknown_types": unknown_types,
                "create_table": create_table,
                "copy_options": CopyOptions(chunk_bytes=CHUNK),
            },
        ),
    )

    return chained.in_report


async def fill(postgres: PostgresSide, table: str, kind: str, expr: str) -> None:
    await postgres.execute(
        [
            f"drop table if exists {S}.{table}",
            f"create table {S}.{table} (id bigint not null, v {kind})",
            f"insert into {S}.{table} select g, {expr} from generate_series(1, 3) g "
            "union all select 4, null",
        ]
    )


async def existing(postgres: PostgresSide, table: str, kind: str) -> None:
    await postgres.execute(
        [
            f"drop table if exists {S}.{table}",
            f"create table {S}.{table} (id bigint not null, v {kind})",
        ]
    )


class TestSchemaStrategies:
    async def test_create_keeps_the_source_types(self, postgres: PostgresSide) -> None:
        report = await land(postgres, "t_create")
        landed = Loaded(postgres, S, "t_create")

        source_types = [
            column[:2] for column in await Loaded(postgres, S, "src").columns()
        ]
        landed_columns = await landed.columns()

        assert f"{ROWS} rows written into {S}.t_create" in report
        assert "schema: create (table is missing)" in report
        assert [column[:2] for column in landed_columns] == source_types
        assert landed_columns[0] == ("id", "bigint", True)
        assert landed_columns[1] == ("name", "character varying(50)", False)
        assert await landed.count() == ROWS
        assert await landed.texts("amount") == await Loaded(postgres, S, "src").texts(
            "amount"
        )

    async def test_second_load_keeps_and_reports_the_check(
        self, postgres: PostgresSide
    ) -> None:
        await land(postgres, "t_twice")
        report = await land(
            postgres,
            "t_twice",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteTruncate(kind="truncate"),
        )

        assert "schema: keep (schema matches)" in report
        assert "- ok amount: ok" in report
        assert "deleted: 0 rows by" in report
        assert await Loaded(postgres, S, "t_twice").count() == ROWS

    async def test_error_if_not_exists_refuses_a_missing_table(
        self, postgres: PostgresSide
    ) -> None:
        with pytest.raises(TransferError, match="table is missing"):
            await land(
                postgres, "t_absent", ErrorIfNotExists(kind="error_if_not_exists")
            )

    async def test_narrower_column_is_a_schema_change(
        self, postgres: PostgresSide
    ) -> None:
        await postgres.execute(
            [
                f"drop table if exists {S}.t_narrow",
                f"create table {S}.t_narrow (id bigint not null, name varchar(10), "
                "amount numeric(18,4), dt timestamp(3), flag boolean, note text)",
            ]
        )
        with pytest.raises(TransferError, match="is shorter than stream"):
            await land(
                postgres,
                "t_narrow",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            )

        report = await land(
            postgres,
            "t_narrow",
            DropAndCreateIfSchemaChanged(kind="drop_and_create_if_schema_changed"),
        )

        assert "schema: drop_then_create" in report
        assert ("name", "character varying(50)", False) in await Loaded(
            postgres, S, "t_narrow"
        ).columns()

    async def test_backup_renames_the_old_table(self, postgres: PostgresSide) -> None:
        await postgres.execute(
            [
                f"drop table if exists {S}.t_bak",
                f"create table {S}.t_bak (id bigint not null, extra int)",
            ]
        )
        report = await land(
            postgres,
            "t_bak",
            BackupAndCreateIfSchemaChanged(kind="backup_and_create_if_schema_changed"),
        )
        tables = await Loaded(postgres, S, "t_bak").tables()

        assert "schema: backup_then_create" in report
        assert "backup: t_bak_bak_" in report
        assert any(name.startswith("t_bak_bak_") for name in tables)


class TestDeleteAndInsert:
    async def test_delete_all_and_where_and_insert_nothing(
        self, postgres: PostgresSide
    ) -> None:
        part = ROWS // 3
        await land(postgres, "t_del")
        report = await land(
            postgres,
            "t_del",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteWhere(kind="delete_where", where=f"id <= {part}"),
            InsertNothing(kind="nothing"),
        )

        assert f"deleted: {part} rows by" in report
        assert "0 rows written" in report
        assert await Loaded(postgres, S, "t_del").count() == ROWS - part

        report = await land(
            postgres,
            "t_del",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            DeleteAll(kind="delete_all"),
        )

        assert f"deleted: {ROWS - part} rows by" in report
        assert await Loaded(postgres, S, "t_del").count() == ROWS


class TestRulesAndDeclarations:
    async def test_rename_and_column_types_shape_the_ddl(
        self, postgres: PostgresSide
    ) -> None:
        rules = ColumnRules(
            rename_columns={"title": "name"}, column_types={"amount": "numeric(20,6)"}
        )
        await land(postgres, "t_rules", rules=rules)
        columns = await Loaded(postgres, S, "t_rules").columns()

        assert ("title", "character varying(50)", False) in columns
        assert ("amount", "numeric(20,6)", False) in columns
        assert (await Loaded(postgres, S, "t_rules").scalars("amount"))[0] == Decimal(
            "0.142900"
        )

    async def test_declared_target_type_takes_part_in_the_check(
        self, postgres: PostgresSide
    ) -> None:
        await existing(postgres, "t_dec", "numeric(18,4)")
        await postgres.execute([f"alter table {S}.t_dec add column extra int"])
        with pytest.raises(TransferError, match="truncates the scale of stream"):
            await land(
                postgres,
                "t_dec",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                rules=ColumnRules(column_types={"v": "numeric(20,6)"}),
                select=f"select id, amount as v, 1 as extra from {S}.src",
            )

    async def test_unknown_declared_type_is_refused_before_ddl(
        self, postgres: PostgresSide
    ) -> None:
        with pytest.raises(TransferError, match="does not accept the declared types"):
            await land(
                postgres, "t_bad", rules=ColumnRules(column_types={"note": "no_such"})
            )

    async def test_rule_on_a_missing_column_is_refused(
        self, postgres: PostgresSide
    ) -> None:
        with pytest.raises(TransferError, match="has no field 'nope'"):
            await land(
                postgres, "t_miss", rules=ColumnRules(rename_columns={"x": "nope"})
            )

        with pytest.raises(TransferError, match="column_types: neither the stream"):
            await land(
                postgres, "t_miss2", rules=ColumnRules(column_types={"x": "int"})
            )

    async def test_declaration_on_a_missing_column_is_refused(
        self, postgres: PostgresSide
    ) -> None:
        with pytest.raises(TransferError, match="has no column 'nope'"):
            await land(
                postgres,
                "t_miss3",
                columns=[PgColumnDeclaration(name="nope", nullable=False)],
            )


class TestUnknownTypes:
    """enum: сервер отдаёт только OID, текста типа нет — ошибка с подсказкой,
    varchar по стратегии, точный тип по column_types или по type_text."""

    async def test_enum_without_a_type_is_refused_with_the_oid(
        self, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_en", "mood", "'sad'::mood")
        with pytest.raises(TransferError, match=r"source type oid \d+; declare"):
            await land(postgres, "t_en", select=f"select * from {S}.s_en")

    async def test_enum_lands_as_varchar_or_as_declared(
        self, postgres: PostgresSide
    ) -> None:
        await fill(postgres, "s_en2", "mood", "'happy'::mood")
        await land(
            postgres,
            "t_en_var",
            select=f"select * from {S}.s_en2",
            unknown_types=FallbackAsVarchar(kind="fallback_as_varchar"),
        )
        await land(
            postgres,
            "t_en_typed",
            select=f"select * from {S}.s_en2",
            rules=ColumnRules(column_types={"v": f"{S}.mood"}),
        )
        await land(
            postgres,
            "t_en_named",
            select=f"select * from {S}.s_en2",
            columns=[PgColumnDeclaration(name="v", type_text=f"{S}.mood")],
        )
        source = Loaded(postgres, S, "s_en2")

        assert (await Loaded(postgres, S, "t_en_var").columns())[1] == (
            "v",
            "character varying",
            False,
        )
        assert (await Loaded(postgres, S, "t_en_typed").columns())[1][1] == f"{S}.mood"
        assert (await Loaded(postgres, S, "t_en_named").columns())[1][1] == f"{S}.mood"
        assert await Loaded(postgres, S, "t_en_named").texts("v") == await source.texts(
            "v"
        )

    async def test_unnamed_enum_cannot_be_verified_but_loads(
        self, postgres: PostgresSide
    ) -> None:
        """OID enum источника на приёмнике не значит ничего, даже если это
        тот же сервер: без имени — предупреждение, данные едут текстом."""
        await fill(postgres, "s_en3", "mood", "'sad'::mood")
        await existing(postgres, "t_en3", "mood")
        report = await land(
            postgres,
            "t_en3",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            select=f"select * from {S}.s_en3",
        )

        assert (
            "- warning v: type cannot be verified, the source named no type" in report
        )
        assert await Loaded(postgres, S, "t_en3").texts("v") == [
            "sad",
            "sad",
            "sad",
            None,
        ]

    async def test_named_enum_is_compared_by_name_on_the_target(
        self, postgres: PostgresSide
    ) -> None:
        """Имя типа из контракта приёмник разбирает у себя и сравнивает свой
        OID с каталогом: тот же enum — ok, другой enum с теми же метками —
        ошибка."""
        await fill(postgres, "s_en4", "mood", "'happy'::mood")
        await existing(postgres, "t_en4", "mood")
        await existing(postgres, "t_en4_other", "mood2")
        named = [PgColumnDeclaration(name="v", type_text=f"{S}.mood")]
        report = await land(
            postgres,
            "t_en4",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            select=f"select * from {S}.s_en4",
            columns=named,
        )

        assert "- ok v: ok" in report

        with pytest.raises(
            TransferError, match=f"type differs: stream {S}.mood, table"
        ):
            await land(
                postgres,
                "t_en4_other",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                select=f"select * from {S}.s_en4",
                columns=named,
            )


class TestExactTypes:
    """Сверка по OID и typmod: другой тип — ошибка, уже typmod — ошибка,
    шире — предупреждение."""

    @pytest.fixture(autouse=True)
    def newest(self, postgres: PostgresSide) -> None:
        only_newest(postgres)

    @pytest.mark.parametrize(
        ("source_kind", "expr", "target_kind", "expected"),
        [
            (
                "uuid",
                "gen_random_uuid()",
                "text",
                "type differs: stream uuid, table text",
            ),
            ("json", "'{}'::json", "jsonb", "type differs: stream json, table jsonb"),
            ("time(6)", "time '12:34:56.123456'", "time(0)", "is coarser than stream"),
            ("timetz", "timetz '12:00:00+03'", "time", "type differs"),
            (
                "inet",
                "'10.0.0.1'::inet",
                "cidr",
                "type differs: stream inet, table cidr",
            ),
            ("int4range", "int4range(1, 3)", "int8range", "type differs"),
            ("bit(8)", "B'10101010'", "bit(4)", "is shorter than stream"),
            ("numeric(20,6)", "g / 7.0", "numeric(18,4)", "truncates the scale"),
            ("text", "'abc'", "varchar(20)", "type differs"),
        ],
    )
    async def test_mismatch_is_refused(
        self,
        postgres: PostgresSide,
        source_kind: str,
        expr: str,
        target_kind: str,
        expected: str,
    ) -> None:
        name = source_kind.split("(", maxsplit=1)[0]
        await fill(postgres, f"s_x_{name}", source_kind, expr)
        await existing(postgres, f"t_x_{name}", target_kind)
        with pytest.raises(TransferError, match=expected):
            await land(
                postgres,
                f"t_x_{name}",
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                select=f"select * from {S}.s_x_{name}",
            )

    @pytest.mark.parametrize(
        ("source_kind", "expr", "target_kind", "expected"),
        [
            ("varchar(10)", "'abc'", "varchar(20)", "is longer than stream"),
            ("numeric(18,4)", "g / 7.0", "numeric(20,6)", "is wider than stream"),
            ("timestamp(0)", "now()", "timestamp(6)", "is finer than stream"),
        ],
    )
    async def test_wider_target_only_warns(
        self,
        postgres: PostgresSide,
        source_kind: str,
        expr: str,
        target_kind: str,
        expected: str,
    ) -> None:
        name = source_kind.split("(", maxsplit=1)[0]
        await fill(postgres, f"s_w_{name}", source_kind, expr)
        await existing(postgres, f"t_w_{name}", target_kind)
        report = await land(
            postgres,
            f"t_w_{name}",
            ErrorIfSchemaChanged(kind="error_if_schema_changed"),
            select=f"select * from {S}.s_w_{name}",
        )

        assert "- warning v:" in report
        assert expected in report

    async def test_created_table_keeps_every_builtin_type(
        self, postgres: PostgresSide
    ) -> None:
        await postgres.execute(
            [
                f"drop table if exists {S}.s_all",
                f"create table {S}.s_all (id bigint not null, u uuid, j json, "
                "jb jsonb, b bytea, t time(3), tz timetz, iv interval, ip inet, "
                "mac macaddr, m money, r int4range, a text[], bt bit(3), "
                "vb varbit(8), x xml, tsv tsvector, pt point, pg polygon, "
                "lsn pg_lsn, rc regclass, n numeric, d date, f real, dp float8)",
                f"insert into {S}.s_all values (1, gen_random_uuid(), '{{}}', "
                "'{}', '\\\\x00', '01:02:03.123', '01:02:03+03', '1 day', "
                "'10.0.0.1', '08:00:2b:01:02:03', 1.5, '[1,3)', array['a'], "
                "B'101', B'1', '<a/>', 'a b', '(1,2)', '((0,0),(1,1),(1,0))', "
                "'0/16B3748', 'pg_class', 1.5, '2024-01-01', 1.5, 1e300)",
            ]
        )
        await land(postgres, "t_all", select=f"select * from {S}.s_all")
        source = Loaded(postgres, S, "s_all")
        target = Loaded(postgres, S, "t_all")

        assert await target.columns() == await source.columns()
        names = [column[0] for column in await source.columns()][1:]
        for column in names:
            assert await target.texts(column) == await source.texts(column)


class TestOlderTarget:
    """Тип источника, которого нет на версии приёмника: OID встроенных типов
    одинаковы на всех версиях, но самого типа на старом сервере нет — отказ
    с подсказкой от приёмника до загрузки."""

    async def test_missing_type_on_the_target_is_a_clear_error(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        if postgres.version < 140000:
            pytest.skip("multirange needs 14")

        older = _older_than(140000)
        if older is None:
            pytest.skip("no postgres older than 14 on the stand")

        target = PostgresSide(older, S)
        await target.connect()
        await target.recreate_schema()
        await fill(postgres, "s_old", "int4multirange", "'{[1,3)}'::int4multirange")
        pumps = Pumps(postgres=postgres.profile, postgres_target=target.profile)
        try:
            with pytest.raises(
                TransferError,
                match='no such type: type "int4multirange" does not exist',
            ):
                await pumps.chain(
                    Leg(
                        "pg_stream_out",
                        {
                            "sql": f"select * from {S}.s_old",
                            "wire": StreamWire.CSV,
                            "columns": [],
                            "copy_options": CopyOptions(chunk_bytes=CHUNK),
                        },
                    ),
                    Leg(
                        "pg_stream_in",
                        {
                            "schema_name": S,
                            "table_name": "t_old",
                            "schema_strategy": CreateIfNotExists(
                                kind="create_if_not_exists"
                            ),
                            "delete_strategy": DeleteNothing(kind="nothing"),
                            "insert_strategy": InsertFull(kind="full"),
                            "rules": ColumnRules(),
                            "unknown_types": FailOnUnknown(kind="fail_on_unknown"),
                            "copy_options": CopyOptions(chunk_bytes=CHUNK),
                        },
                    ),
                )

            assert await Loaded(target, S, "t_old").tables() == []
        finally:
            await target.drop()


class TestCreateTemplate:
    """Шаблон create table: особенности таблицы пишет вызывающий, приёмник
    подставляет экранированные схему, имя и колонки; шаблон без
    обязательной переменной или с чужой — отказ до любого DDL."""

    WITH_OPTIONS: ClassVar[str] = (
        "create table {schema_name}.{table_name} ({columns}) with (fillfactor = 70)"
    )
    DISTRIBUTED: ClassVar[str] = (
        "create table {schema_name}.{table_name} ({columns}) distributed by (id)"
    )
    ESCAPED: ClassVar[str] = (
        "create table {schema_name}.{table_name} ({columns}) -- {{not a variable}}"
    )

    async def test_with_options_reach_reloptions(self, postgres: PostgresSide) -> None:
        report = await land(postgres, "t_tpl_with", create_table=self.WITH_OPTIONS)
        landed = Loaded(postgres, S, "t_tpl_with")

        assert f"{ROWS} rows written" in report
        assert await landed.count() == ROWS
        options = await landed.aggregate(
            "(select reloptions::text from pg_class "
            f"where oid = '{S}.t_tpl_with'::regclass)"
        )
        assert options == "{fillfactor=70}"

    async def test_distributed_by_on_greenplum(self, postgres: PostgresSide) -> None:
        if not postgres.greenplum:
            pytest.skip("distributed by is Greenplum only")

        report = await land(postgres, "t_tpl_dist", create_table=self.DISTRIBUTED)
        landed = Loaded(postgres, S, "t_tpl_dist")

        assert f"{ROWS} rows written" in report
        policy = await landed.aggregate(
            "(select distkey::text from gp_distribution_policy "
            f"where localoid = '{S}.t_tpl_dist'::regclass)"
        )
        assert policy == "1"

    async def test_escaped_braces_stay_literal(self, postgres: PostgresSide) -> None:
        report = await land(postgres, "t_tpl_esc", create_table=self.ESCAPED)

        assert f"{ROWS} rows written" in report
        assert await Loaded(postgres, S, "t_tpl_esc").count() == ROWS

    async def test_template_without_columns_is_refused(
        self, postgres: PostgresSide
    ) -> None:
        with pytest.raises(TransferError, match="lacks \\['columns'\\]"):
            await land(
                postgres,
                "t_tpl_no_cols",
                create_table="create table {schema_name}.{table_name} ()",
            )

        assert (
            "t_tpl_no_cols" not in await Loaded(postgres, S, "t_tpl_no_cols").tables()
        )

    async def test_unknown_variable_is_refused(self, postgres: PostgresSide) -> None:
        with pytest.raises(TransferError, match="unknown variable \\{owner\\}"):
            await land(
                postgres,
                "t_tpl_unknown",
                create_table=(
                    "create table {schema_name}.{table_name} ({columns}) "
                    "tablespace {owner}"
                ),
            )

    async def test_server_error_in_options_is_reported(
        self, postgres: PostgresSide
    ) -> None:
        with pytest.raises(psycopg.Error, match="no_such_option"):
            await land(
                postgres,
                "t_tpl_bad",
                create_table=(
                    "create table {schema_name}.{table_name} ({columns}) "
                    "with (no_such_option = 1)"
                ),
            )


class TestTsvWire:
    async def test_text_layout_lands_the_same_rows(
        self, postgres: PostgresSide
    ) -> None:
        await fill(
            postgres,
            "s_tsv",
            "text",
            "'tab\\there' || chr(10) || 'back\\\\slash' || g",
        )
        report = await land(
            postgres,
            "t_tsv",
            select=f"select * from {S}.s_tsv",
            wire=StreamWire.TSV,
        )

        assert "4 rows written" in report
        assert await Loaded(postgres, S, "t_tsv").texts("v") == await Loaded(
            postgres, S, "s_tsv"
        ).texts("v")


class TestBinaryWire:
    """COPY binary: тот же сервер — грузится байт в байт; enum — отказ, у
    пользовательских типов в binary OID инстанса; другая мажорная версия —
    отказ с подсказкой взять csv."""

    async def test_binary_lands_the_same_rows_on_the_same_server(
        self, postgres: PostgresSide
    ) -> None:
        report = await land(postgres, "t_bin", wire=StreamWire.BINARY)
        source = Loaded(postgres, S, "src")
        target = Loaded(postgres, S, "t_bin")

        assert f"{ROWS} rows written" in report
        for column in ("name", "amount", "dt", "flag", "note"):
            assert await target.texts(column) == await source.texts(column)

    async def test_user_type_is_refused_in_binary(self, postgres: PostgresSide) -> None:
        await fill(postgres, "s_bin_en", "mood", "'sad'::mood")
        with pytest.raises(TransferError, match="outside the built-in registry"):
            await land(
                postgres,
                "t_bin_en",
                select=f"select * from {S}.s_bin_en",
                wire=StreamWire.BINARY,
            )

    async def test_other_major_version_is_refused(self, postgres: PostgresSide) -> None:
        only_newest(postgres)
        older = _older_than(postgres.version // 10000 * 10000)
        if older is None:
            pytest.skip("no older postgres on the stand")

        target = PostgresSide(older, S)
        await target.connect()
        await target.recreate_schema()
        pumps = Pumps(postgres=postgres.profile, postgres_target=target.profile)
        try:
            with pytest.raises(TransferError, match="major versions differ"):
                await pumps.chain(
                    Leg(
                        "pg_stream_out",
                        {
                            "sql": SELECT,
                            "wire": StreamWire.BINARY,
                            "columns": [],
                            "copy_options": CopyOptions(chunk_bytes=CHUNK),
                        },
                    ),
                    Leg(
                        "pg_stream_in",
                        {
                            "schema_name": S,
                            "table_name": "t_bin_old",
                            "schema_strategy": CreateIfNotExists(
                                kind="create_if_not_exists"
                            ),
                            "delete_strategy": DeleteNothing(kind="nothing"),
                            "insert_strategy": InsertFull(kind="full"),
                            "rules": ColumnRules(),
                            "unknown_types": FailOnUnknown(kind="fail_on_unknown"),
                            "copy_options": CopyOptions(chunk_bytes=CHUNK),
                        },
                    ),
                )
        finally:
            await target.drop()


class TestDescribeCost:
    """Контракт берётся PQprepare + PQdescribePrepared без выполнения: запрос
    на часы выполнения описывается за миллисекунды, а счётчики чтения базы
    не растут."""

    HEAVY: ClassVar[str] = (
        "with recursive r(n) as (select 1 union all select n + 1 from r "
        "where n < 200000000) "
        "select r.n, pg_sleep(0.01) as slept, md5(r.n::text) as h, "
        "sum(r.n) over () as total from r "
        "join generate_series(1, 1000000) g on g = r.n"
    )
    COUNTERS: ClassVar[str] = (
        "select tup_returned, tup_fetched, blks_read from pg_stat_database "
        "where datname = current_database()"
    )
    BUDGET_SECONDS: ClassVar[float] = 0.5

    async def test_describe_neither_plans_nor_reads(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        async with await AsyncPostgresPool.dedicated(postgres.profile) as conn:
            counters = PgQueryBuilder().add(self.COUNTERS).build()
            cursor = await conn.execute(counters.text)
            before = await cursor.fetchone()
            started = time.perf_counter()
            contract = await PgCopyOut(conn).contract(self.HEAVY, ())
            elapsed = time.perf_counter() - started
            cursor = await conn.execute(counters.text)
            after = await cursor.fetchone()

        names = [column.name for column in contract.columns]

        assert names == ["n", "slept", "h", "total"]
        assert elapsed < self.BUDGET_SECONDS
        assert after == before


class BrokenSink(Sink):
    """Порт, который рвёт трубу на втором кадре rows."""

    LIMIT: ClassVar[int] = 2

    def emit(self, head: Any, body: Any = b"") -> None:
        super().emit(head, body)
        if len(self.heads) >= self.LIMIT:
            raise BrokenPipeError("receiver went away")


class TestCopyOutLoop:
    """COPY TO STDOUT идёт циклом libpq в потоке: миллион строк не медленнее
    полутора psql, ошибка сервера посреди COPY доходит своим классом,
    обрыв трубы отменяет запрос, и соединение остаётся рабочим."""

    ROWS: ClassVar[int] = 1_000_000
    SELECT: ClassVar[str] = (
        "select g as id, g * 7 as v, 'name_' || g as name, now() as ts, "
        "g::numeric / 3 as amount from generate_series(1, {rows}) g"
    )
    CHUNK: ClassVar[int] = 262144
    PSQL_RATIO: ClassVar[float] = 1.5

    def _psql_seconds(self, postgres: PostgresSide, select: str) -> float:
        psql = shutil.which("psql")
        if psql is None:
            pytest.skip("psql is not installed on the host")

        settings = postgres.profile.conn_settings()
        env = {"PGPASSWORD": str(settings.get("password", "")), "PATH": "/usr/bin:/bin"}
        argv = [
            psql,
            "-h",
            str(settings["host"]),
            "-p",
            str(settings["port"]),
            "-U",
            str(settings["user"]),
            "-d",
            str(settings["dbname"]),
            "-c",
            f"\\copy ({select}) to '/dev/null' with (format csv)",
        ]
        started = time.perf_counter()
        completed = subprocess.run(
            argv, env=env, capture_output=True, text=True, check=False
        )
        elapsed = time.perf_counter() - started

        assert completed.returncode == 0, completed.stderr
        assert f"COPY {self.ROWS}" in completed.stdout

        return elapsed

    async def test_million_rows_keep_up_with_psql(self, postgres: PostgresSide) -> None:
        only_newest(postgres)
        select = self.SELECT.format(rows=self.ROWS)
        sink = Sink()
        async with await AsyncPostgresPool.dedicated(postgres.profile) as conn:
            copy_out = PgCopyOut(conn)
            contract = await copy_out.contract(select, ())
            started = time.perf_counter()
            report = await copy_out.stream(
                select,
                PgCopyLayout.CSV,
                contract,
                self.CHUNK,
                TransferOutbound(SinkOutbound(sink)),
            )
            elapsed = time.perf_counter() - started

        psql_elapsed = self._psql_seconds(postgres, select)
        print(
            f"\ncopy out {self.ROWS} rows: ours {elapsed:.2f}s "
            f"({self.ROWS / elapsed:,.0f} rows/s), psql {psql_elapsed:.2f}s, "
            f"frames {len(sink.heads) - 1}"
        )

        assert report.status == f"COPY {self.ROWS}"
        assert sink.data().count(b"\n") == self.ROWS
        assert len(sink.heads) - 1 <= len(sink.data()) // self.CHUNK + 1
        assert elapsed <= psql_elapsed * self.PSQL_RATIO

    async def test_server_error_mid_copy_keeps_its_class(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        select = "select g, 1 / (g - 5000) as bad from generate_series(1, 10000) g"
        async with await AsyncPostgresPool.dedicated(postgres.profile) as conn:
            copy_out = PgCopyOut(conn)
            contract = await copy_out.contract(select, ())
            with pytest.raises(psycopg.errors.DivisionByZero):
                await copy_out.stream(
                    select,
                    PgCopyLayout.CSV,
                    contract,
                    4096,
                    TransferOutbound(SinkOutbound(Sink())),
                )

            await conn.rollback()
            cursor = await conn.execute("select 1")

            assert await cursor.fetchone() == (1,)

    async def test_broken_pipe_cancels_and_frees_the_connection(
        self, postgres: PostgresSide
    ) -> None:
        only_newest(postgres)
        select = self.SELECT.format(rows=self.ROWS)
        async with await AsyncPostgresPool.dedicated(postgres.profile) as conn:
            copy_out = PgCopyOut(conn)
            contract = await copy_out.contract(select, ())
            started = time.perf_counter()
            with pytest.raises(BrokenPipeError):
                await copy_out.stream(
                    select,
                    PgCopyLayout.CSV,
                    contract,
                    4096,
                    TransferOutbound(SinkOutbound(BrokenSink())),
                )
            elapsed = time.perf_counter() - started

            await conn.rollback()
            cursor = await conn.execute("select 2")

            assert await cursor.fetchone() == (2,)

        assert elapsed < 1.0


class TestTypeRules:
    """Правила сверки postgres на встроенном реестре psycopg без сервера."""

    NUMERIC: ClassVar[int] = 1700
    VARCHAR: ClassVar[int] = 1043
    TEXT: ClassVar[int] = 25
    TIMESTAMP: ClassVar[int] = 1114

    @staticmethod
    def stream(oid: int, typmod: int, nullable: bool = True) -> PgStreamColumn:
        return PgStreamColumn(
            name="v",
            oid=oid,
            typmod=typmod,
            type_text="t",
            known="t",
            nullable=nullable,
            resolved=False,
        )

    @staticmethod
    def table(oid: int, typmod: int, not_null: bool = False) -> PgCatalogColumn:
        return PgCatalogColumn(
            position=1,
            name="v",
            oid=oid,
            typmod=typmod,
            table_oid=0,
            attnum=2,
            size=-1,
            not_null=not_null,
        )

    @staticmethod
    def numeric(precision: int, scale: int) -> int:
        return ((precision << 16) | scale) + 4

    @pytest.mark.parametrize(
        ("stream", "table", "level", "message"),
        [
            ((TEXT, -1), (TEXT, -1), "ok", "ok"),
            ((TEXT, -1), (VARCHAR, 24), "error", "type differs"),
            ((VARCHAR, 14), (VARCHAR, 24), "warning", "is longer than stream"),
            ((VARCHAR, 24), (VARCHAR, 14), "error", "is shorter than stream"),
            ((VARCHAR, -1), (VARCHAR, 14), "warning", "limits the length"),
            ((TIMESTAMP, 0), (TIMESTAMP, -1), "warning", "is finer than stream"),
            ((TIMESTAMP, -1), (TIMESTAMP, 0), "error", "is coarser than stream"),
        ],
    )
    def test_compare(
        self, stream: tuple[int, int], table: tuple[int, int], level: str, message: str
    ) -> None:
        from psycopg.postgres import types as registry

        verdict = PgTypeRules(registry).compare(
            self.stream(*stream), self.table(*table)
        )

        assert verdict.level.value == level
        assert message in verdict.message

    def test_numeric_digits(self) -> None:
        from psycopg.postgres import types as registry

        rules = PgTypeRules(registry)
        wider = rules.compare(
            self.stream(self.NUMERIC, self.numeric(18, 4)),
            self.table(self.NUMERIC, self.numeric(20, 6)),
        )
        scale = rules.compare(
            self.stream(self.NUMERIC, self.numeric(20, 6)),
            self.table(self.NUMERIC, self.numeric(18, 4)),
        )
        digits = rules.compare(
            self.stream(self.NUMERIC, self.numeric(20, 4)),
            self.table(self.NUMERIC, self.numeric(18, 4)),
        )

        assert wider.level.value == "warning"
        assert "truncates the scale" in scale.message
        assert "fewer integer digits" in digits.message

    def test_nullable(self) -> None:
        from psycopg.postgres import types as registry

        rules = PgTypeRules(registry)
        refused = rules.compare(
            self.stream(self.TEXT, -1), self.table(self.TEXT, -1, True)
        )
        warned = rules.compare(
            self.stream(self.TEXT, -1, nullable=False), self.table(self.TEXT, -1)
        )

        assert refused.level.value == "error"
        assert warned.level.value == "warning"


class TestRegistryHint:
    def test_missing_pair_is_a_clear_error(self) -> None:
        from boba.db.postgres.transfer import PgTransfers
        from boba.toolkit.transfer import Engine

        with pytest.raises(
            TransferError, match="no transfer from oracle into postgres"
        ):
            PgTransfers({}).pair(Engine.ORACLE)
