"""Diff снимков: статусы только у затронутых сущностей."""

from __future__ import annotations

from uuid import UUID

from sample_catalog import Sample

from boba.catalog import (
    AddLayer,
    CatalogDiff,
    CatalogSnapshot,
    ChangeStatus,
    EntityKind,
    EntityRef,
    Layer,
    OperationList,
    RemoveDataset,
    RemoveFlow,
    SetColumn,
    SetDataset,
)


class TestDiff:
    def test_identical_snapshots_have_empty_diff(
        self, snapshot: CatalogSnapshot
    ) -> None:
        diff = CatalogDiff.between(snapshot, snapshot)

        assert diff.is_empty()
        assert (
            diff.status_of(EntityRef(kind=EntityKind.LAYER, id=UUID(int=1)))
            is ChangeStatus.UNCHANGED
        )

    def test_set_marks_only_changed_entities(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        renamed = sample.stg_orders.model_copy(update={"description": "clean orders"})
        same = sample.raw_orders.model_copy()
        retyped = sample.dm_sales_total.model_copy(update={"type": "decimal(18,2)"})

        changed = OperationList(
            root=(
                SetDataset(dataset=renamed),
                SetDataset(dataset=same),
                SetColumn(column=retyped),
            )
        ).apply(snapshot)
        diff = CatalogDiff.between(snapshot, changed)

        assert len(diff.entries) == 2
        assert diff.status_of(EntityRef.of(renamed)) is ChangeStatus.MODIFIED
        assert diff.status_of(EntityRef.of(retyped)) is ChangeStatus.MODIFIED
        assert diff.status_of(EntityRef.of(same)) is ChangeStatus.UNCHANGED

    def test_added_and_removed(self, sample: Sample, snapshot: CatalogSnapshot) -> None:
        ods = Layer(id=UUID(int=4), name="ods")
        changed = OperationList(
            root=(
                AddLayer(layer=ods),
                RemoveFlow(id=sample.flow_sales.id),
                RemoveDataset(id=sample.dm_sales.id),
            )
        ).apply(snapshot)

        diff = CatalogDiff.between(snapshot, changed)

        assert diff.status_of(EntityRef.of(ods)) is ChangeStatus.ADDED
        assert diff.status_of(EntityRef.of(sample.flow_sales)) is ChangeStatus.REMOVED
        assert diff.status_of(EntityRef.of(sample.dm_sales)) is ChangeStatus.REMOVED
        assert (
            diff.status_of(EntityRef.of(sample.dm_sales_total)) is ChangeStatus.REMOVED
        )
        assert (
            diff.status_of(EntityRef.of(sample.dm_sales_customer))
            is ChangeStatus.REMOVED
        )
        assert len(diff.entries) == 5

    def test_diff_is_directional(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        ods = Layer(id=UUID(int=4), name="ods")
        changed = OperationList(root=(AddLayer(layer=ods),)).apply(snapshot)

        forward = CatalogDiff.between(snapshot, changed)
        backward = CatalogDiff.between(changed, snapshot)

        assert forward.status_of(EntityRef.of(ods)) is ChangeStatus.ADDED
        assert backward.status_of(EntityRef.of(ods)) is ChangeStatus.REMOVED

    def test_diff_serializes_for_api(
        self, sample: Sample, snapshot: CatalogSnapshot
    ) -> None:
        ods = Layer(id=UUID(int=4), name="ods")
        changed = OperationList(root=(AddLayer(layer=ods),)).apply(snapshot)

        dumped = CatalogDiff.between(snapshot, changed).model_dump(mode="json")

        assert dumped == {
            "entries": [
                {"ref": {"kind": "layer", "id": str(ods.id)}, "status": "added"}
            ]
        }
