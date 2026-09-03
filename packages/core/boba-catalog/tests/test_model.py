"""Модели каталога: проверка снимка целиком, вид загрузки, подписи и выборки."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError
from sample_catalog import Sample

from boba.catalog import (
    CatalogInvariantError,
    CatalogSnapshot,
    Column,
    Dataset,
    EntityKind,
    EntityRef,
    Flow,
    Layer,
    LoadField,
    LoadFieldType,
    LoadKind,
    LoadSpec,
)


class TestSnapshotCheck:
    def test_empty_snapshot_is_consistent(self) -> None:
        CatalogSnapshot.empty().check()

    def test_check_lists_every_violation_at_once(self, sample: Sample) -> None:
        broken = CatalogSnapshot(
            layers={
                sample.raw.id: sample.raw,
                UUID(int=90): Layer(id=UUID(int=90), name="raw"),
            },
            datasets={
                sample.raw_orders.id: sample.raw_orders,
                UUID(int=91): Dataset(
                    id=UUID(int=91), layer_id=UUID(int=92), name="lost"
                ),
            },
            columns={sample.raw_orders_id.id: sample.raw_orders_id},
            load_kinds={sample.hashkey.id: sample.hashkey},
            flows={
                sample.flow_orders.id: sample.flow_orders,
            },
        )

        with pytest.raises(CatalogInvariantError) as info:
            broken.check()

        violations = info.value.violations
        assert "duplicate layer name 'raw'" in violations
        assert f"dataset 'lost' refers to missing layer {UUID(int=92)}" in violations
        target = sample.stg_orders.id
        assert (
            f"flow dataset 'orders' -> dataset {target} "
            f"refers to missing dataset {target}" in violations
        )
        assert len(violations) == 3

    def test_snapshot_from_rows_needs_conformed_values(self, sample: Sample) -> None:
        raw_flow = Flow.model_validate(
            {
                "id": str(sample.flow_orders.id),
                "from_dataset_id": str(sample.raw_orders.id),
                "to_dataset_id": str(sample.stg_orders.id),
                "load": {
                    "kind_id": str(sample.hashkey.id),
                    "values": {"hash_columns": [str(sample.raw_orders_id.id)]},
                },
            }
        )
        base = sample.snapshot()

        conformed = base.conformed(raw_flow)
        rebuilt = base.replaced(conformed)

        rebuilt.check()
        assert rebuilt.flows[raw_flow.id].load.values["hash_columns"] == (
            sample.raw_orders_id.id,
        )


class TestLoadKind:
    def test_duplicate_field_names_rejected_on_parse(self) -> None:
        with pytest.raises(ValidationError) as info:
            LoadKind(
                id=UUID(int=1),
                name="period",
                fields=(
                    LoadField(name="from", type=LoadFieldType.TEXT, required=True),
                    LoadField(name="from", type=LoadFieldType.INT, required=False),
                ),
            )

        assert "duplicate field names ['from']" in str(info.value)

    def test_field_type_accepts_only_its_storage_shape(self) -> None:
        column_id = UUID(int=5)

        assert LoadFieldType.TEXT.accepts("x")
        assert not LoadFieldType.TEXT.accepts(1)
        assert LoadFieldType.INT.accepts(3)
        assert not LoadFieldType.INT.accepts(True)
        assert LoadFieldType.BOOL.accepts(False)
        assert not LoadFieldType.BOOL.accepts(0)
        assert LoadFieldType.COLUMN.accepts(column_id)
        assert not LoadFieldType.COLUMN.accepts(str(column_id))
        assert LoadFieldType.COLUMNS.accepts((column_id,))
        assert not LoadFieldType.COLUMNS.accepts(())
        assert not LoadFieldType.COLUMNS.accepts(column_id)

    def test_column_refs_cover_single_and_list_fields(self) -> None:
        kind = LoadKind(
            id=UUID(int=2),
            name="merge",
            fields=(
                LoadField(name="key", type=LoadFieldType.COLUMN, required=True),
                LoadField(name="compare", type=LoadFieldType.COLUMNS, required=False),
                LoadField(name="batch", type=LoadFieldType.INT, required=False),
            ),
        )
        spec = LoadSpec(
            kind_id=kind.id,
            values={
                "key": UUID(int=10),
                "compare": (UUID(int=11), UUID(int=12)),
                "batch": 5,
            },
        )

        refs = list(kind.column_refs(spec))

        assert refs == [
            ("key", UUID(int=10)),
            ("compare", UUID(int=11)),
            ("compare", UUID(int=12)),
        ]
        assert list(kind.violations_of(spec)) == []

    def test_conform_keeps_unknown_fields_for_violation_report(self) -> None:
        kind = LoadKind(id=UUID(int=3), name="full", fields=())
        spec = LoadSpec(kind_id=kind.id, values={"stray": "x"})

        conformed = kind.conform(spec)

        assert conformed.values == {"stray": "x"}
        assert list(kind.violations_of(conformed)) == [
            "unknown field 'stray' of load kind 'full'"
        ]


class TestQueries:
    def test_flows_of_dataset_covers_both_directions(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        flows = list(snapshot.flows_of(sample.stg_orders.id))

        ids: set[UUID] = set()
        for flow in flows:
            ids.add(flow.id)

        assert ids == {sample.flow_orders.id, sample.flow_sales.id}

    def test_flows_using_column(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        using = list(snapshot.flows_using_column(sample.raw_orders_id.id))
        unused = list(snapshot.flows_using_column(sample.raw_orders_amount.id))

        assert using == [sample.flow_orders]
        assert unused == []

    def test_labels_prefer_names_and_fall_back_to_ids(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        assert snapshot.label(EntityRef.of(sample.raw)) == "layer 'raw'"
        assert snapshot.label(EntityRef.of(sample.hashkey)) == "load_kind 'hashkey'"
        assert (
            snapshot.label(EntityRef.of(sample.flow_orders))
            == "flow dataset 'orders' -> dataset 'orders'"
        )
        assert snapshot.label(EntityRef(kind=EntityKind.COLUMN, id=UUID(int=404))) == (
            f"column {UUID(int=404)}"
        )

    def test_entity_kind_of_every_entity(self, sample: Sample) -> None:
        assert EntityKind.of(sample.raw) is EntityKind.LAYER
        assert EntityKind.of(sample.raw_orders) is EntityKind.DATASET
        assert EntityKind.of(sample.raw_orders_id) is EntityKind.COLUMN
        assert EntityKind.of(sample.full) is EntityKind.LOAD_KIND
        assert EntityKind.of(sample.flow_sales) is EntityKind.FLOW

    def test_models_are_frozen_and_strict(self, sample: Sample) -> None:
        with pytest.raises(ValidationError):
            sample.raw.name = "other"  # type: ignore[misc]

        with pytest.raises(ValidationError):
            Column.model_validate(
                {
                    "id": str(UUID(int=1)),
                    "dataset_id": str(UUID(int=2)),
                    "name": "x",
                    "type": "int",
                    "nullable": False,
                    "is_key": False,
                    "position": -1,
                }
            )
