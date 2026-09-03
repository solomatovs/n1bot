"""Применение операций к снимку: полный каталог, отказы по зависимостям, разбор JSON."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import ValidationError
from sample_catalog import Sample

from boba.catalog import (
    AddColumn,
    AddDataset,
    AddFlow,
    AddLayer,
    AddLoadKind,
    CatalogOp,
    CatalogOpError,
    CatalogOpKind,
    CatalogSnapshot,
    Column,
    Dataset,
    Flow,
    Layer,
    LoadField,
    LoadFieldType,
    LoadKind,
    LoadSpec,
    OperationList,
    RemoveColumn,
    RemoveDataset,
    RemoveFlow,
    RemoveLayer,
    RemoveLoadKind,
    SetColumn,
    SetDataset,
    SetFlow,
    SetLayer,
)


def _apply(snapshot: CatalogSnapshot, *ops: CatalogOp) -> CatalogSnapshot:
    return OperationList(root=ops).apply(snapshot)


def _failure(snapshot: CatalogSnapshot, *ops: CatalogOp) -> CatalogOpError:
    with pytest.raises(CatalogOpError) as info:
        _apply(snapshot, *ops)

    return info.value


def _flow_with_values(sample: Sample, values: dict[str, object]) -> Flow:
    load = LoadSpec.model_validate({"kind_id": sample.hashkey.id, "values": values})
    return sample.flow_orders.model_copy(update={"load": load})


class TestApply:
    def test_full_sequence_builds_consistent_snapshot(self, sample: Sample) -> None:
        snapshot = sample.snapshot()

        snapshot.check()
        assert len(snapshot.layers) == 3
        assert len(snapshot.datasets) == 5
        assert len(snapshot.columns) == 13
        assert len(snapshot.load_kinds) == 2
        assert len(snapshot.flows) == 3

    def test_apply_does_not_mutate_input(self, sample: Sample) -> None:
        empty = CatalogSnapshot.empty()
        built = OperationList(root=tuple(sample.ops())).apply(empty)

        assert not empty.layers
        assert built.layers

        renamed = sample.raw.model_copy(update={"name": "landing"})
        changed = _apply(built, SetLayer(layer=renamed))

        assert built.layers[sample.raw.id].name == "raw"
        assert changed.layers[sample.raw.id].name == "landing"

    def test_empty_list_returns_same_snapshot(self, snapshot: CatalogSnapshot) -> None:
        assert OperationList(root=()).apply(snapshot) is snapshot

    def test_error_carries_index_of_failed_operation(self, sample: Sample) -> None:
        ops = sample.ops()
        ops.insert(4, AddLayer(layer=Layer(id=UUID(int=99), name="raw")))

        failure = _failure(CatalogSnapshot.empty(), *ops)

        assert failure.index == 4
        assert failure.op.op is CatalogOpKind.ADD_LAYER
        assert "duplicate layer name 'raw'" in failure.reason


class TestAddSet:
    def test_add_with_taken_id_is_refused(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        duplicate = Dataset(
            id=sample.raw_orders.id, layer_id=sample.dm.id, name="again"
        )

        failure = _failure(snapshot, AddDataset(dataset=duplicate))

        assert failure.index == 0
        assert "dataset 'orders' already exists" in failure.reason

    def test_set_with_unknown_id_is_refused(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        stranger = sample.raw_orders.model_copy(update={"id": UUID(int=777)})

        failure = _failure(snapshot, SetDataset(dataset=stranger))

        assert "not found" in failure.reason
        assert str(UUID(int=777)) in failure.reason

    def test_set_replaces_entity_entirely(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        replacement = Dataset(
            id=sample.stg_orders.id,
            layer_id=sample.stg.id,
            name="orders_clean",
            description="deduplicated",
        )

        changed = _apply(snapshot, SetDataset(dataset=replacement))

        assert changed.datasets[sample.stg_orders.id] == replacement
        assert changed.datasets[sample.stg_orders.id].tags == ()

    def test_set_column_moving_to_foreign_dataset_breaks_flow(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        moved = sample.raw_orders_id.model_copy(
            update={"dataset_id": sample.dm_sales.id}
        )

        failure = _failure(snapshot, SetColumn(column=moved))

        assert "outside the flow datasets" in failure.reason


class TestRemove:
    def test_remove_layer_refused_while_it_holds_datasets(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        failure = _failure(snapshot, RemoveLayer(id=sample.raw.id))

        assert "layer 'raw' still holds 2 dataset(s)" in failure.reason

    def test_remove_layer_passes_after_explicit_cleanup(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        cleaned = _apply(
            snapshot,
            RemoveFlow(id=sample.flow_orders.id),
            RemoveFlow(id=sample.flow_customers.id),
            RemoveDataset(id=sample.raw_orders.id),
            RemoveDataset(id=sample.raw_customers.id),
            RemoveLayer(id=sample.raw.id),
        )

        assert sample.raw.id not in cleaned.layers
        assert len(cleaned.datasets) == 3
        assert len(cleaned.columns) == 7

    def test_remove_dataset_refused_while_flows_use_it(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        failure = _failure(snapshot, RemoveDataset(id=sample.stg_orders.id))

        assert "dataset 'orders' is used by 2 flow(s)" in failure.reason

    def test_remove_dataset_takes_its_columns_along(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        changed = _apply(
            snapshot,
            RemoveFlow(id=sample.flow_sales.id),
            RemoveDataset(id=sample.dm_sales.id),
        )

        assert sample.dm_sales.id not in changed.datasets
        assert sample.dm_sales_customer.id not in changed.columns
        assert sample.dm_sales_total.id not in changed.columns
        assert len(changed.columns) == 11

    def test_remove_load_kind_refused_while_flows_use_it(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        failure = _failure(snapshot, RemoveLoadKind(id=sample.full.id))

        assert "load_kind 'full' is used by 2 flow(s)" in failure.reason

        changed = _apply(
            snapshot,
            RemoveFlow(id=sample.flow_customers.id),
            RemoveFlow(id=sample.flow_sales.id),
            RemoveLoadKind(id=sample.full.id),
        )

        assert sample.full.id not in changed.load_kinds

    def test_remove_column_refused_while_flow_values_reference_it(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        failure = _failure(snapshot, RemoveColumn(id=sample.raw_orders_id.id))

        assert "column 'order_id' is referenced by 1 flow(s)" in failure.reason

    def test_remove_column_passes_after_flow_points_elsewhere(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        rehashed = _flow_with_values(
            sample, {"hash_columns": [sample.stg_orders_id.id]}
        )

        changed = _apply(
            snapshot,
            SetFlow(flow=rehashed),
            RemoveColumn(id=sample.raw_orders_id.id),
        )

        assert sample.raw_orders_id.id not in changed.columns

    def test_remove_unknown_id_is_refused(self, snapshot: CatalogSnapshot) -> None:
        failure = _failure(snapshot, RemoveFlow(id=UUID(int=555)))

        assert failure.reason == f"flow {UUID(int=555)} not found"


class TestInvariants:
    def test_dataset_needs_existing_layer(self, snapshot: CatalogSnapshot) -> None:
        orphan = Dataset(id=UUID(int=60), layer_id=UUID(int=61), name="orphan")

        failure = _failure(snapshot, AddDataset(dataset=orphan))

        assert (
            f"dataset 'orphan' refers to missing layer {UUID(int=61)}" in failure.reason
        )

    def test_column_needs_existing_dataset(self, snapshot: CatalogSnapshot) -> None:
        orphan = Column(
            id=UUID(int=62),
            dataset_id=UUID(int=63),
            name="ghost",
            type="text",
            nullable=True,
            is_key=False,
            position=0,
        )

        failure = _failure(snapshot, AddColumn(column=orphan))

        assert "column 'ghost' refers to missing dataset" in failure.reason

    def test_flow_needs_existing_datasets_and_kind(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        dangling = Flow(
            id=UUID(int=64),
            from_dataset_id=sample.raw_orders.id,
            to_dataset_id=UUID(int=65),
            load=LoadSpec(kind_id=UUID(int=66), values={}),
        )

        failure = _failure(snapshot, AddFlow(flow=dangling))

        assert f"refers to missing dataset {UUID(int=65)}" in failure.reason
        assert f"refers to missing load kind {UUID(int=66)}" in failure.reason

    def test_flow_cannot_loop_on_one_dataset(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        loop = Flow(
            id=UUID(int=67),
            from_dataset_id=sample.stg_orders.id,
            to_dataset_id=sample.stg_orders.id,
            load=LoadSpec(kind_id=sample.full.id, values={}),
        )

        failure = _failure(snapshot, AddFlow(flow=loop))

        assert "loops on the same dataset" in failure.reason

    def test_dataset_name_unique_within_layer_only(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        same_layer = Dataset(id=UUID(int=68), layer_id=sample.raw.id, name="orders")
        other_layer = Dataset(id=UUID(int=69), layer_id=sample.dm.id, name="orders")

        failure = _failure(snapshot, AddDataset(dataset=same_layer))
        assert "duplicate dataset name 'orders' in layer 'raw'" in failure.reason

        changed = _apply(snapshot, AddDataset(dataset=other_layer))
        assert other_layer.id in changed.datasets

    def test_column_name_and_position_unique_within_dataset(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        same_name = sample.raw_orders_id.model_copy(
            update={"id": UUID(int=70), "position": 9}
        )
        same_position = sample.raw_orders_id.model_copy(
            update={"id": UUID(int=71), "name": "x"}
        )

        by_name = _failure(snapshot, AddColumn(column=same_name))
        assert "duplicate column name 'order_id' in dataset 'orders'" in by_name.reason

        by_position = _failure(snapshot, AddColumn(column=same_position))
        assert "duplicate column position 0 in dataset 'orders'" in by_position.reason

    def test_load_kind_name_unique(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        twin = LoadKind(id=UUID(int=72), name="full", fields=())

        failure = _failure(snapshot, AddLoadKind(load_kind=twin))

        assert "duplicate load kind name 'full'" in failure.reason


class TestFlowValues:
    def test_unknown_field_is_refused(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        flow = _flow_with_values(
            sample, {"hash_columns": [sample.raw_orders_id.id], "window": "1d"}
        )

        failure = _failure(snapshot, SetFlow(flow=flow))

        assert "unknown field 'window' of load kind 'hashkey'" in failure.reason

    def test_missing_required_field_is_refused(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        flow = _flow_with_values(sample, {"batch": 10})

        failure = _failure(snapshot, SetFlow(flow=flow))

        assert (
            "required field 'hash_columns' of load kind 'hashkey' is missing"
            in failure.reason
        )

    def test_column_of_foreign_dataset_is_refused(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        flow = _flow_with_values(
            sample, {"hash_columns": [sample.dm_sales_customer.id]}
        )

        failure = _failure(snapshot, SetFlow(flow=flow))

        assert "field 'hash_columns' refers to column 'customer_id'" in failure.reason
        assert "outside the flow datasets" in failure.reason

    def test_unknown_column_is_refused(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        flow = _flow_with_values(sample, {"hash_columns": [UUID(int=80)]})

        failure = _failure(snapshot, SetFlow(flow=flow))

        assert f"refers to missing column {UUID(int=80)}" in failure.reason

    def test_wrong_value_types_are_refused(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        text_batch = _flow_with_values(
            sample, {"hash_columns": [sample.raw_orders_id.id], "batch": "many"}
        )
        failure = _failure(snapshot, SetFlow(flow=text_batch))
        assert (
            "field 'batch' of load kind 'hashkey' expects int, got str"
            in failure.reason
        )

        bool_batch = _flow_with_values(
            sample, {"hash_columns": [sample.raw_orders_id.id], "batch": True}
        )
        failure = _failure(snapshot, SetFlow(flow=bool_batch))
        assert "expects int, got bool" in failure.reason

        single = _flow_with_values(sample, {"hash_columns": sample.raw_orders_id.id})
        failure = _failure(snapshot, SetFlow(flow=single))
        assert "expects columns, got UUID" in failure.reason

        empty = _flow_with_values(sample, {"hash_columns": []})
        failure = _failure(snapshot, SetFlow(flow=empty))
        assert "expects columns, got list of 0" in failure.reason

    def test_column_reference_as_string_is_conformed(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        keyed = LoadKind(
            id=UUID(int=81),
            name="keyed",
            fields=(
                LoadField(name="key", type=LoadFieldType.COLUMN, required=True),
                LoadField(
                    name="hash_columns", type=LoadFieldType.COLUMNS, required=True
                ),
            ),
        )
        flow = Flow.model_validate(
            {
                "id": str(UUID(int=82)),
                "from_dataset_id": str(sample.raw_orders.id),
                "to_dataset_id": str(sample.stg_orders.id),
                "load": {
                    "kind_id": str(keyed.id),
                    "values": {
                        "key": str(sample.raw_orders_id.id),
                        "hash_columns": [str(sample.stg_orders_id.id)],
                    },
                },
            }
        )

        changed = _apply(snapshot, AddLoadKind(load_kind=keyed), AddFlow(flow=flow))

        stored = changed.flows[flow.id].load.values
        assert stored["key"] == sample.raw_orders_id.id
        assert stored["hash_columns"] == (sample.stg_orders_id.id,)

    def test_column_reference_with_garbage_string_is_refused(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        keyed = LoadKind(
            id=UUID(int=83),
            name="keyed",
            fields=(LoadField(name="key", type=LoadFieldType.COLUMN, required=True),),
        )
        flow = Flow.model_validate(
            {
                "id": str(UUID(int=84)),
                "from_dataset_id": str(sample.raw_orders.id),
                "to_dataset_id": str(sample.stg_orders.id),
                "load": {"kind_id": str(keyed.id), "values": {"key": "order_id"}},
            }
        )

        failure = _failure(snapshot, AddLoadKind(load_kind=keyed), AddFlow(flow=flow))

        assert failure.index == 1
        assert (
            "field 'key' of load kind 'keyed' is not a column id: 'order_id'"
            in failure.reason
        )


class TestJson:
    def test_json_round_trip_is_lossless(self, sample: Sample) -> None:
        original = OperationList(root=tuple(sample.ops()))

        text = original.model_dump_json()
        parsed = OperationList.model_validate_json(text)

        assert json.loads(parsed.model_dump_json()) == json.loads(text)
        assert parsed.apply(CatalogSnapshot.empty()) == sample.snapshot()

    def test_python_dicts_with_string_ids_apply_like_models(
        self, sample: Sample
    ) -> None:
        raw = json.loads(OperationList(root=tuple(sample.ops())).model_dump_json())

        parsed = OperationList.model_validate(raw)

        assert parsed.apply(CatalogSnapshot.empty()) == sample.snapshot()

    def test_unknown_op_and_extra_keys_are_rejected(self, sample: Sample) -> None:
        with pytest.raises(ValidationError):
            OperationList.model_validate(
                [{"op": "rename_layer", "id": str(sample.raw.id)}]
            )

        with pytest.raises(ValidationError):
            OperationList.model_validate(
                [{"op": "remove_layer", "id": str(sample.raw.id), "force": True}]
            )

        with pytest.raises(ValidationError):
            OperationList.model_validate(
                [{"op": "add_layer", "layer": {"id": str(sample.raw.id), "name": ""}}]
            )
