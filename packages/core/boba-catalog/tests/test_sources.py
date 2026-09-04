"""Снимки источников: инварианты, дерево любой глубины, diff версий,
операции ручного источника, разбор из JSON."""

from __future__ import annotations

import pytest

from boba.catalog import (
    CatalogInvariantError,
    ChangeStatus,
    ObjectKind,
    ObjectRef,
    PartKind,
    SourceDiff,
    TreeKind,
)
from boba.catalog.samples import SampleIds
from boba.db.clickhouse.snapshot import ChTable
from boba.db.clickhouse.snapshot_sample import ChSample
from boba.db.postgres.snapshot import (
    PgColumn,
    PgIndex,
    PgRelation,
    PgSequence,
)
from boba.db.postgres.snapshot_sample import PgSample

SOURCE_ID = SampleIds.POSTGRES
CH_SOURCE_ID = SampleIds.CLICKHOUSE


@pytest.fixture
def ch() -> ChSample:
    return ChSample()


class TestInvariants:
    def test_sample_snapshots_are_consistent(self, pg: PgSample, ch: ChSample) -> None:
        pg.snapshot().check()
        ch.snapshot().check()
        assert pg.snapshot().objects_count() == 9
        assert ch.snapshot().objects_count() == 4

    def test_duplicate_key_is_reported(self, pg: PgSample) -> None:
        snapshot = pg.snapshot()
        twice = snapshot.model_copy(
            update={"relations": (*snapshot.relations, pg.orders)}
        )

        with pytest.raises(CatalogInvariantError) as error:
            twice.check()

        assert "duplicate relation prod/public/orders" in str(error.value)

    def test_column_without_relation_is_reported(self, pg: PgSample) -> None:
        orphan = PgColumn(
            database="prod",
            schema_name="public",
            relation="ghost",
            name="id",
            ordinal=1,
            type="integer",
        )
        snapshot = pg.snapshot()
        broken = snapshot.model_copy(update={"columns": (*snapshot.columns, orphan)})

        with pytest.raises(CatalogInvariantError) as error:
            broken.check()

        assert "column prod/public/ghost/id has no parent" in str(error.value)

    def test_snapshot_round_trips_through_json(
        self, pg: PgSample, ch: ChSample
    ) -> None:
        for snapshot in (pg.snapshot(), ch.snapshot()):
            raw = snapshot.model_dump_json()
            assert type(snapshot).model_validate_json(raw) == snapshot


class TestPostgresTree:
    def test_levels_from_databases_to_partitions(self, pg: PgSample) -> None:
        snapshot = pg.snapshot()

        databases = snapshot.children(SOURCE_ID, ())
        assert [node.label for node in databases] == ["prod"]
        assert databases[0].kind is TreeKind.DATABASE
        assert databases[0].children_count == 2

        schemas = snapshot.children(SOURCE_ID, ("prod",))
        assert [node.label for node in schemas] == ["etl", "public"]
        assert schemas[0].comment == "Загрузка"

        groups = snapshot.children(SOURCE_ID, ("prod", "public"))
        assert [(node.label, node.children_count) for node in groups] == [
            ("tables", 2),
            ("views", 1),
            ("sequences", 1),
            ("types", 1),
        ]

        tables = snapshot.children(SOURCE_ID, ("prod", "public", "tables"))
        assert [node.label for node in tables] == ["customers", "orders"]
        orders = tables[1]
        assert orders.kind is TreeKind.OBJECT
        assert orders.detail == "partitioned"
        assert orders.children_count == 1
        assert orders.ref == ObjectRef(
            source_id=SOURCE_ID,
            kind=ObjectKind.RELATION,
            path=("prod", "public", "orders"),
        )

        partitions = snapshot.children(
            SOURCE_ID, ("prod", "public", "tables", "orders")
        )
        assert [node.label for node in partitions] == ["orders_2026"]
        assert partitions[0].detail.startswith("FOR VALUES")
        assert partitions[0].ref is not None
        assert partitions[0].ref.path == ("prod", "public", "orders_2026")

        assert (
            snapshot.children(SOURCE_ID, ("prod", "public", "tables", "orders", "x"))
            == []
        )

    def test_routines_are_grouped_and_overloads_are_distinct(
        self, pg: PgSample
    ) -> None:
        snapshot = pg.snapshot()

        groups = snapshot.children(SOURCE_ID, ("prod", "etl"))
        assert [(node.label, node.children_count) for node in groups] == [
            ("functions", 2),
            ("procedures", 1),
        ]

        functions = snapshot.children(SOURCE_ID, ("prod", "etl", "functions"))
        assert [node.label for node in functions] == [
            "hash_key(text)",
            "hash_key(text, text)",
        ]
        assert functions[0].ref is not None
        assert functions[0].ref.kind is ObjectKind.ROUTINE
        assert functions[0].ref.path == ("prod", "etl", "hash_key", "text")

        procedures = snapshot.children(SOURCE_ID, ("prod", "etl", "procedures"))
        assert procedures[0].detail == "procedure"

    def test_object_lookups(self, pg: PgSample) -> None:
        snapshot = pg.snapshot()
        columns = list(snapshot.columns_of(("prod", "public", "orders")))
        assert [column.name for column in columns] == ["id", "amount", "created_at"]
        assert [
            c.name for c in snapshot.constraints_of(("prod", "public", "orders"))
        ] == ["orders_pkey"]
        assert [i.name for i in snapshot.indexes_of(("prod", "public", "orders"))] == [
            "orders_created_idx"
        ]
        args = list(snapshot.args_of(("prod", "etl", "load_orders", "date")))
        assert [(arg.name, arg.type) for arg in args] == [("day", "date")]
        assert snapshot.relation(("prod", "public", "ghost")) is None


class TestClickHouseTree:
    def test_levels_from_databases_to_objects(self, ch: ChSample) -> None:
        snapshot = ch.snapshot()

        databases = snapshot.children(CH_SOURCE_ID, ())
        assert [
            (node.label, node.detail, node.children_count) for node in databases
        ] == [("dwh", "Atomic", 4)]

        groups = snapshot.children(CH_SOURCE_ID, ("dwh",))
        assert [(node.label, node.children_count) for node in groups] == [
            ("tables", 1),
            ("views", 1),
            ("materialized", 1),
            ("dictionaries", 1),
        ]

        tables = snapshot.children(CH_SOURCE_ID, ("dwh", "tables"))
        assert tables[0].label == "events"
        assert tables[0].detail == "MergeTree"
        assert tables[0].ref == ObjectRef(
            source_id=CH_SOURCE_ID, kind=ObjectKind.TABLE, path=("dwh", "events")
        )

        dictionaries = snapshot.children(CH_SOURCE_ID, ("dwh", "dictionaries"))
        assert dictionaries[0].detail == "Hashed"
        assert dictionaries[0].ref is not None
        assert dictionaries[0].ref.kind is ObjectKind.DICTIONARY

        columns = list(snapshot.columns_of(("dwh", "events")))
        assert [column.name for column in columns] == ["ts", "user_id", "payload"]
        attributes = list(snapshot.attributes_of(("dwh", "users")))
        assert [attribute.name for attribute in attributes] == ["name"]


class TestDiff:
    def test_added_removed_and_field_level_changes(self, pg: PgSample) -> None:
        diff = SourceDiff.between(SOURCE_ID, pg.snapshot(), pg.next_version())

        by_path = {entry.ref.path: entry for entry in diff.entries}
        assert by_path[("prod", "public", "customers")].status is ChangeStatus.REMOVED
        assert by_path[("prod", "public", "returns")].status is ChangeStatus.ADDED

        orders = by_path[("prod", "public", "orders")]
        assert orders.status is ChangeStatus.MODIFIED
        assert orders.fields == ()
        parts = {(part.part, part.name): part for part in orders.parts}
        amount = parts[(PartKind.COLUMN, "amount")]
        assert amount.status is ChangeStatus.MODIFIED
        assert [(f.field, f.was, f.now) for f in amount.fields] == [
            ("type", "numeric(10,2)", "numeric(12,2)")
        ]
        assert parts[(PartKind.COLUMN, "note")].status is ChangeStatus.ADDED

        procedure = by_path[("prod", "etl", "load_orders", "date")]
        assert procedure.status is ChangeStatus.MODIFIED
        assert [f.field for f in procedure.fields] == ["body"]

        unchanged = ObjectRef(
            source_id=SOURCE_ID,
            kind=ObjectKind.RELATION,
            path=("prod", "public", "v_orders"),
        )
        assert diff.status_of(unchanged) is ChangeStatus.UNCHANGED
        assert ("prod", "public") in diff.touched_prefixes()
        assert ("prod",) in diff.touched_prefixes()

    def test_identical_versions_have_no_entries(self, ch: ChSample) -> None:
        diff = SourceDiff.between(CH_SOURCE_ID, ch.snapshot(), ch.snapshot())
        assert diff.entries == ()

    def test_volatile_statistics_are_not_changes(
        self, pg: PgSample, ch: ChSample
    ) -> None:
        """Число строк, размер и последнее значение последовательности меняются
        без изменения структуры: вторая синхронизация той же базы даёт пустой
        diff, а настоящее изменение поля всё так же видно."""
        before = pg.snapshot()
        relations: list[PgRelation] = []
        for relation in before.relations:
            grown_bytes = relation.total_bytes + 147456
            relations.append(
                relation.model_copy(
                    update={"row_estimate": 504, "total_bytes": grown_bytes}
                )
            )

        indexes: list[PgIndex] = []
        for index in before.indexes:
            indexes.append(index.model_copy(update={"total_bytes": 65536}))

        sequences: list[PgSequence] = []
        for sequence in before.sequences:
            sequences.append(sequence.model_copy(update={"last_value": 777}))

        grown = before.model_copy(
            update={
                "relations": tuple(relations),
                "indexes": tuple(indexes),
                "sequences": tuple(sequences),
            }
        )
        assert SourceDiff.between(SOURCE_ID, before, grown).entries == ()

        ch_before = ch.snapshot()
        tables: list[ChTable] = []
        for table in ch_before.tables:
            tables.append(
                table.model_copy(
                    update={
                        "total_rows": 10,
                        "total_bytes": 20,
                        "metadata_modified_at": "2026-09-05 00:00:00",
                    }
                )
            )

        ch_grown = ch_before.model_copy(update={"tables": tuple(tables)})
        assert SourceDiff.between(CH_SOURCE_ID, ch_before, ch_grown).entries == ()

        owned: list[PgRelation] = []
        for relation in relations:
            owned.append(relation.model_copy(update={"owner": "other"}))

        changed = SourceDiff.between(
            SOURCE_ID, before, grown.model_copy(update={"relations": tuple(owned)})
        )
        statuses: set[ChangeStatus] = set()
        fields: set[str] = set()
        for entry in changed.entries:
            statuses.add(entry.status)
            for field in entry.fields:
                fields.add(field.field)

        assert statuses == {ChangeStatus.MODIFIED}
        assert fields == {"owner"}

    def test_kinds_must_match(self, pg: PgSample, ch: ChSample) -> None:
        with pytest.raises(Exception, match="cannot diff postgres against clickhouse"):
            SourceDiff.between(SOURCE_ID, pg.snapshot(), ch.snapshot())
