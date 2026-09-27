"""Ядро сверки схем: вердикты по типам, объединение потока, таблицы и
правил, планы стратегий схемы — на данных, без баз."""

from __future__ import annotations

import pyarrow
import pytest

from boba.toolkit.arrow import ArrowColumns, SourceFields
from boba.toolkit.sync import (
    ColumnSpec,
    ColumnType,
    SchemaMatcher,
    SyncError,
    TimeUnit,
    TypeComparer,
    TypeFamily,
    Verdict,
)
from boba.toolkit.transfer import (
    BackupAndCreateIfSchemaChanged,
    ColumnRules,
    CreateIfNotExists,
    DoNothing,
    DropAndCreate,
    DropAndCreateIfSchemaChanged,
    ErrorIfNotExists,
    ErrorIfSchemaChanged,
    SchemaAction,
    SchemaCheck,
)

INT32 = ColumnType(TypeFamily.INTEGER, "int32", bits=32)
INT64 = ColumnType(TypeFamily.INTEGER, "int64", bits=64)
DEC_18_4 = ColumnType(TypeFamily.DECIMAL, "decimal128(18, 4)", precision=18, scale=4)
DEC_20_4 = ColumnType(TypeFamily.DECIMAL, "decimal128(20, 4)", precision=20, scale=4)
DEC_18_2 = ColumnType(TypeFamily.DECIMAL, "decimal128(18, 2)", precision=18, scale=2)
TEXT = ColumnType(TypeFamily.STRING, "large_string")
TS_US = ColumnType(TypeFamily.TIMESTAMP, "timestamp[us]", unit=TimeUnit.MICROSECOND)
TS_S = ColumnType(TypeFamily.TIMESTAMP, "timestamp[s]", unit=TimeUnit.SECOND)
TS_TZ = ColumnType(
    TypeFamily.TIMESTAMP, "timestamp[us, tz=UTC]", unit=TimeUnit.MICROSECOND, zoned=True
)


OTHER_INET = ColumnType(TypeFamily.OTHER, "inet")
OTHER_CIDR = ColumnType(TypeFamily.OTHER, "cidr")
OTHER_OID = ColumnType(TypeFamily.OTHER, "oid 16437")


def spec(
    name: str,
    kind: ColumnType,
    nullable: bool = True,
    length: int = 0,
    source_type: str = "",
) -> ColumnSpec:
    return ColumnSpec(
        name=name,
        kind=kind,
        nullable=nullable,
        position=0,
        char_length=length,
        source_type=source_type,
    )


def exact_levels(
    stream: list[ColumnSpec], table: list[ColumnSpec]
) -> dict[str, tuple[Verdict, str]]:
    comparer = TypeComparer(True)
    verdicts: dict[str, tuple[Verdict, str]] = {}
    for source, target in zip(stream, table, strict=True):
        verdict = comparer.compare(source, target)
        verdicts[source.name] = (verdict.level, verdict.message)

    return verdicts


def levels(stream: list[ColumnSpec], table: list[ColumnSpec]) -> dict[str, Verdict]:
    diff = SchemaMatcher(ColumnRules(), False).diff(stream, table, {})
    verdicts: dict[str, Verdict] = {}
    for match in diff.matches:
        verdicts[match.name] = match.verdict(
            SchemaMatcher(ColumnRules(), False)._comparer
        ).level

    return verdicts


class TestTypeVerdicts:
    @pytest.mark.parametrize(
        ("source", "target", "level"),
        [
            (spec("c", INT32), spec("c", INT64), Verdict.WARNING),
            (spec("c", INT64), spec("c", INT32), Verdict.ERROR),
            (spec("c", INT64), spec("c", INT64), Verdict.OK),
            (spec("c", DEC_18_4), spec("c", DEC_20_4), Verdict.WARNING),
            (spec("c", DEC_20_4), spec("c", DEC_18_4), Verdict.ERROR),
            (spec("c", DEC_18_4), spec("c", DEC_18_2), Verdict.ERROR),
            (spec("c", TEXT, length=200), spec("c", TEXT, length=100), Verdict.ERROR),
            (spec("c", TEXT, length=100), spec("c", TEXT, length=200), Verdict.WARNING),
            (spec("c", TEXT, length=100), spec("c", TEXT), Verdict.OK),
            (spec("c", TEXT), spec("c", TEXT, length=100), Verdict.WARNING),
            (spec("c", TS_US), spec("c", TS_S), Verdict.ERROR),
            (spec("c", TS_S), spec("c", TS_US), Verdict.WARNING),
            (spec("c", TS_US), spec("c", TS_TZ), Verdict.ERROR),
            (spec("c", INT64), spec("c", TEXT), Verdict.ERROR),
            (
                spec("c", INT64, nullable=True),
                spec("c", INT64, nullable=False),
                Verdict.ERROR,
            ),
            (
                spec("c", INT64, nullable=False),
                spec("c", INT64, nullable=True),
                Verdict.WARNING,
            ),
        ],
    )
    def test_compatibility(
        self, source: ColumnSpec, target: ColumnSpec, level: Verdict
    ) -> None:
        assert levels([source], [target]) == {"c": level}

    @pytest.mark.parametrize(
        ("source", "target", "level", "message"),
        [
            (
                spec("c", OTHER_INET, source_type="inet"),
                spec("c", OTHER_INET, source_type="inet"),
                Verdict.OK,
                "ok",
            ),
            (
                spec("c", OTHER_INET, source_type="inet"),
                spec("c", OTHER_CIDR, source_type="cidr"),
                Verdict.ERROR,
                "type differs: stream inet, table cidr",
            ),
            (
                spec("c", OTHER_OID),
                spec("c", OTHER_INET, source_type="inet"),
                Verdict.WARNING,
                "type cannot be verified, the source named no type: "
                "stream oid 16437, table inet",
            ),
            (
                spec("c", TEXT, source_type="json"),
                spec("c", TEXT, source_type="jsonb"),
                Verdict.WARNING,
                "type differs: stream json, table jsonb",
            ),
            (
                spec("c", TEXT, source_type="text"),
                spec("c", TEXT, source_type="text"),
                Verdict.OK,
                "ok",
            ),
        ],
    )
    def test_exact_same_engine_compares_the_type_text(
        self, source: ColumnSpec, target: ColumnSpec, level: Verdict, message: str
    ) -> None:
        assert exact_levels([source], [target]) == {"c": (level, message)}

    def test_other_across_engines_cannot_be_verified(self) -> None:
        stream = [spec("c", OTHER_INET, source_type="inet")]
        table = [spec("c", OTHER_CIDR, source_type="cidr")]
        verdict = TypeComparer(False).compare(stream[0], table[0])

        assert verdict.level is Verdict.WARNING
        assert verdict.message.startswith("type cannot be verified across engines")


class TestMatcher:
    def test_stream_only_and_table_only_columns_are_errors(self) -> None:
        verdicts = levels(
            [spec("a", INT64), spec("b", TEXT)], [spec("a", INT64), spec("z", TEXT)]
        )

        assert verdicts == {"a": Verdict.OK, "b": Verdict.ERROR, "z": Verdict.ERROR}

    def test_rename_maps_the_stream_field_onto_the_table_column(self) -> None:
        diff = SchemaMatcher(
            ColumnRules(rename_columns={"created_at": "created"}), False
        ).diff([spec("created", TS_US)], [spec("created_at", TS_US)], {})

        assert diff.errors() == []
        assert diff.table_spec().names() == ["created_at"]
        assert diff.table_spec().source_names() == ["created"]

    def test_rename_from_a_missing_field_is_refused(self) -> None:
        with pytest.raises(SyncError, match="has no field 'nope'"):
            SchemaMatcher(ColumnRules(rename_columns={"x": "nope"}), False).diff(
                [spec("a", INT64)], [], {}
            )

    def test_render_lists_every_column(self) -> None:
        diff = SchemaMatcher(ColumnRules(), False).diff(
            [spec("a", INT64)], [spec("a", INT32)], {}
        )

        assert diff.render().startswith("- error a:")
        assert diff.check().changed()


class TestSchemaPlans:
    SAME = SchemaCheck(errors=(), warnings=(), lines=())
    DRIFT = SchemaCheck(errors=("column a: narrower",), warnings=(), lines=())

    @pytest.mark.parametrize(
        ("strategy", "exists", "diff", "action"),
        [
            (
                CreateIfNotExists(kind="create_if_not_exists"),
                False,
                SAME,
                SchemaAction.CREATE,
            ),
            (
                CreateIfNotExists(kind="create_if_not_exists"),
                True,
                DRIFT,
                SchemaAction.KEEP,
            ),
            (
                ErrorIfNotExists(kind="error_if_not_exists"),
                False,
                SAME,
                SchemaAction.FAIL,
            ),
            (
                ErrorIfNotExists(kind="error_if_not_exists"),
                True,
                DRIFT,
                SchemaAction.KEEP,
            ),
            (
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                True,
                DRIFT,
                SchemaAction.FAIL,
            ),
            (
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                True,
                SAME,
                SchemaAction.KEEP,
            ),
            (
                ErrorIfSchemaChanged(kind="error_if_schema_changed"),
                False,
                SAME,
                SchemaAction.FAIL,
            ),
            (
                DropAndCreateIfSchemaChanged(kind="drop_and_create_if_schema_changed"),
                True,
                DRIFT,
                SchemaAction.DROP_THEN_CREATE,
            ),
            (
                DropAndCreateIfSchemaChanged(kind="drop_and_create_if_schema_changed"),
                True,
                SAME,
                SchemaAction.KEEP,
            ),
            (
                BackupAndCreateIfSchemaChanged(
                    kind="backup_and_create_if_schema_changed"
                ),
                True,
                DRIFT,
                SchemaAction.BACKUP_THEN_CREATE,
            ),
            (
                BackupAndCreateIfSchemaChanged(
                    kind="backup_and_create_if_schema_changed"
                ),
                False,
                SAME,
                SchemaAction.CREATE,
            ),
            (
                DropAndCreate(kind="drop_and_create", cascade=True),
                True,
                SAME,
                SchemaAction.DROP_THEN_CREATE,
            ),
            (DoNothing(kind="do_nothing"), False, DRIFT, SchemaAction.KEEP),
        ],
    )
    def test_plan(
        self, strategy, exists: bool, diff: SchemaCheck, action: SchemaAction
    ) -> None:
        assert strategy.plan(exists, diff).action is action

    def test_cascade_travels_with_the_plan(self) -> None:
        plan = DropAndCreate(kind="drop_and_create", cascade=True).plan(True, self.SAME)

        assert plan.cascade


class TestArrowColumns:
    def test_stream_schema_becomes_specs_with_metadata(self) -> None:
        fields = SourceFields("postgres")
        schema = pyarrow.schema(
            [
                fields.field("id", pyarrow.int64(), False, "bigint", 0),
                fields.field(
                    "name", pyarrow.large_string(), True, "character varying(200)", 200
                ),
                fields.field(
                    "amount", pyarrow.decimal128(18, 4), True, "numeric(18,4)", 0
                ),
                fields.field(
                    "ts",
                    pyarrow.timestamp("us", "UTC"),
                    True,
                    "timestamp with time zone",
                    0,
                ),
                pyarrow.field("plain", pyarrow.date32()),
            ]
        )
        specs = ArrowColumns().specs(schema)

        assert [s.name for s in specs] == ["id", "name", "amount", "ts", "plain"]
        assert specs[0].kind == INT64
        assert not specs[0].nullable
        assert specs[1].char_length == 200
        assert specs[1].source_type == "character varying(200)"
        assert specs[2].kind.precision == 18
        assert specs[3].kind.zoned
        assert specs[4].kind.family is TypeFamily.DATE
        assert specs[4].source_type == ""
