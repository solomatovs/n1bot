"""Процесс над ссылками: инварианты снимка, проверка по источнику, операции с
отказами, diff, устаревание при новой версии источника, разбор из JSON."""

from __future__ import annotations

from uuid import UUID

import pytest

from boba.catalog import (
    AcceptAll,
    AddFlow,
    AddNode,
    CatalogDiff,
    CatalogInvariantError,
    CatalogOpError,
    CatalogSnapshot,
    ChangeStatus,
    EntityKind,
    EntityRef,
    Flow,
    LoadSpec,
    Node,
    ObjectKind,
    OperationList,
    PinnedSnapshot,
    RemoveFlow,
    RemoveLayer,
    RemoveNode,
    RetargetNode,
    SetNode,
    SnapshotResolver,
    Staleness,
    StaleReason,
)
from boba.catalog.samples import ProcessSample
from boba.db.postgres.snapshot_sample import PgSample


class TestInvariants:
    def test_sample_is_consistent_with_its_source(
        self, snapshot: CatalogSnapshot, resolver: SnapshotResolver
    ) -> None:
        snapshot.check()
        snapshot.check_against(resolver)
        assert snapshot.sources() == {ProcessSample().source_id}

    def test_duplicate_layer_position_and_placed_object(
        self, process: ProcessSample, snapshot: CatalogSnapshot
    ) -> None:
        twin = process.dm.model_copy(update={"id": UUID(int=0x7199), "name": "dm2"})
        with pytest.raises(CatalogInvariantError) as error:
            snapshot.added(twin).check()

        assert "duplicate layer position 1" in str(error.value)

        placed_twice = Node(
            id=UUID(int=0x7299), layer_id=process.dm.id, ref=process.orders.ref
        )
        with pytest.raises(CatalogInvariantError) as error:
            snapshot.added(placed_twice).check()

        assert "is placed twice" in str(error.value)

    def test_flow_values_are_checked_by_kind_and_by_source(
        self,
        process: ProcessSample,
        snapshot: CatalogSnapshot,
        resolver: SnapshotResolver,
    ) -> None:
        wrong_shape = process.flow_orders.model_copy(
            update={
                "load": LoadSpec(
                    kind_id=process.hashkey.id, values={"hash_columns": "id"}
                )
            }
        )
        with pytest.raises(CatalogInvariantError) as error:
            snapshot.replaced(wrong_shape).check()

        assert "expects columns, got str" in str(error.value)

        wrong_side = process.flow_orders.model_copy(
            update={
                "load": LoadSpec(
                    kind_id=process.hashkey.id, values={"hash_columns": ("id", "nope")}
                )
            }
        )
        with pytest.raises(CatalogInvariantError) as error:
            snapshot.replaced(wrong_side).check_against(resolver)

        assert "names column 'nope' that is not on the source side" in str(error.value)

        ghost = process.orders.model_copy(
            update={
                "ref": process.ref(ObjectKind.RELATION, ("prod", "public", "ghost"))
            }
        )
        with pytest.raises(CatalogInvariantError) as error:
            snapshot.replaced(ghost).check_against(resolver)

        assert "points to a missing object" in str(error.value)

    def test_labels_and_lookups(
        self, process: ProcessSample, snapshot: CatalogSnapshot
    ) -> None:
        assert process.customers.label == "clients"
        assert process.orders.label == "orders"
        assert snapshot.node_of(process.orders.ref) == process.orders
        assert [f.id for f in snapshot.flows_of(process.v_orders.id)] == [
            process.flow_orders.id,
            process.flow_customers.id,
        ]
        assert snapshot.label(EntityRef.of(process.flow_orders)) == (
            "flow node 'prod/public/orders' -> node 'prod/public/v_orders'"
        )

    def test_restricted_keeps_inner_flows(
        self, process: ProcessSample, snapshot: CatalogSnapshot
    ) -> None:
        sliced = snapshot.restricted([process.orders.id, process.v_orders.id], [])
        assert set(sliced.nodes) == {process.orders.id, process.v_orders.id}
        assert set(sliced.flows) == {process.flow_orders.id}
        assert set(sliced.load_kinds) == {process.hashkey.id}
        assert set(sliced.layers) == {process.raw.id, process.dm.id}


class TestOperations:
    def test_ops_rebuild_the_sample(
        self, process: ProcessSample, resolver: SnapshotResolver
    ) -> None:
        built = process.ops().apply(CatalogSnapshot.empty(), resolver)
        assert built == process.snapshot()

    def test_rejections_name_the_operation(
        self,
        process: ProcessSample,
        snapshot: CatalogSnapshot,
        resolver: SnapshotResolver,
    ) -> None:
        with pytest.raises(CatalogOpError) as error:
            OperationList(root=(RemoveNode(id=process.orders.id),)).apply(
                snapshot, resolver
            )

        assert error.value.index == 0
        assert "is used by 1 flow(s)" in error.value.reason

        with pytest.raises(CatalogOpError) as error:
            OperationList(root=(RemoveLayer(id=process.raw.id),)).apply(
                snapshot, resolver
            )

        assert "still holds 2 node(s)" in error.value.reason

        moved = process.orders.model_copy(update={"ref": process.customers.ref})
        with pytest.raises(CatalogOpError) as error:
            OperationList(root=(SetNode(node=moved),)).apply(snapshot, resolver)

        assert "use retarget_node" in error.value.reason

    def test_retarget_keeps_flows_and_is_checked_against_the_source(
        self,
        process: ProcessSample,
        snapshot: CatalogSnapshot,
        resolver: SnapshotResolver,
    ) -> None:
        target = process.ref(ObjectKind.RELATION, ("prod", "public", "customers"))
        with pytest.raises(CatalogOpError) as error:
            OperationList(root=(RetargetNode(id=process.orders.id, ref=target),)).apply(
                snapshot, resolver
            )

        assert "is placed twice" in error.value.reason

        elsewhere = process.ref(ObjectKind.RELATION, ("prod", "public", "orders_2026"))
        with pytest.raises(CatalogOpError) as error:
            OperationList(
                root=(RetargetNode(id=process.orders.id, ref=elsewhere),)
            ).apply(snapshot, resolver)

        assert "names column 'id' that is not on the source side" in error.value.reason

        retargeted = OperationList(
            root=(RetargetNode(id=process.customers.id, ref=elsewhere),)
        ).apply(snapshot, resolver)
        assert retargeted.nodes[process.customers.id].ref == elsewhere
        assert process.flow_customers.id in retargeted.flows

    def test_accept_all_skips_source_checks(self, process: ProcessSample) -> None:
        ghost = Node(
            id=UUID(int=0x7299),
            layer_id=process.raw.id,
            ref=process.ref(ObjectKind.RELATION, ("prod", "public", "ghost")),
        )
        built = OperationList(root=(*process.ops().root, AddNode(node=ghost))).apply(
            CatalogSnapshot.empty(), AcceptAll()
        )
        assert ghost.id in built.nodes

    def test_stale_process_is_fixed_one_operation_at_a_time(
        self, process: ProcessSample, snapshot: CatalogSnapshot
    ) -> None:
        """Источник ушёл вперёд и customers пропала: уже существующее
        расхождение не мешает снять поток и узел, а новое — отвергается."""
        resolver = SnapshotResolver({process.source_id: PgSample().next_version()})
        assert any(
            "customers" in violation
            for violation in snapshot.source_violations(resolver)
        )

        fixed = OperationList(
            root=(
                RemoveFlow(id=process.flow_customers.id),
                RemoveNode(id=process.customers.id),
            )
        ).apply(snapshot, resolver)
        assert process.customers.id not in fixed.nodes

        ghost = process.ref(ObjectKind.RELATION, ("prod", "public", "ghost"))
        with pytest.raises(CatalogOpError) as error:
            OperationList(root=(RetargetNode(id=process.orders.id, ref=ghost),)).apply(
                snapshot, resolver
            )

        assert "ghost" in error.value.reason
        assert "customers" not in error.value.reason

    def test_operations_parse_from_json(self, process: ProcessSample) -> None:
        layer_id = "00000000-0000-0000-0000-000000007101"
        raw = (
            f'[{{"op": "add_layer", "layer": {{"id": "{layer_id}",'
            ' "name": "raw", "position": 0}},'
            ' {"op": "add_node", "node": {"id": "00000000-0000-0000-0000-000000007201",'
            f' "layer_id": "{layer_id}",'
            ' "ref": {"source_id": "00000000-0000-0000-0000-000000005001",'
            ' "kind": "relation", "path": ["prod", "public", "orders"]},'
            ' "alias": "o"}}]'
        )
        ops = OperationList.model_validate_json(raw)
        built = ops.apply(CatalogSnapshot.empty(), AcceptAll())
        assert built.nodes[process.orders.id].alias == "o"
        assert built.nodes[process.orders.id].ref == process.orders.ref

        routine_flow = Flow.model_validate(
            {
                "id": "00000000-0000-0000-0000-000000007499",
                "from_node_id": str(process.orders.id),
                "to_node_id": str(process.v_orders.id),
                "load": {
                    "kind_id": str(process.hashkey.id),
                    "values": {
                        "hash_columns": ["id"],
                        "implemented_by": {
                            "source_id": str(process.source_id),
                            "kind": "routine",
                            "path": ["prod", "etl", "load_orders", "date"],
                        },
                    },
                },
            }
        )
        assert routine_flow.load.values["implemented_by"] == process.load_orders.ref
        assert routine_flow.load.values["hash_columns"] == ("id",)


class TestDiffAndStaleness:
    def test_diff_marks_changed_entities(
        self,
        process: ProcessSample,
        snapshot: CatalogSnapshot,
        resolver: SnapshotResolver,
    ) -> None:
        extra = Flow(
            id=UUID(int=0x7499),
            from_node_id=process.customers.id,
            to_node_id=process.orders.id,
            load=LoadSpec(kind_id=process.full.id, values={}),
        )
        renamed = process.customers.model_copy(update={"alias": "buyers"})
        other = OperationList(root=(AddFlow(flow=extra), SetNode(node=renamed))).apply(
            snapshot, resolver
        )
        diff = CatalogDiff.between(snapshot, other)
        assert (
            diff.status_of(EntityRef(kind=EntityKind.FLOW, id=extra.id))
            is ChangeStatus.ADDED
        )
        assert diff.status_of(EntityRef.of(renamed)) is ChangeStatus.MODIFIED
        assert diff.status_of(EntityRef.of(process.orders)) is ChangeStatus.UNCHANGED

    def test_new_source_version_marks_nodes_and_flows(
        self, pg: PgSample, process: ProcessSample, snapshot: CatalogSnapshot
    ) -> None:
        pinned = {process.source_id: PinnedSnapshot(version=1, snapshot=pg.snapshot())}
        latest = {
            process.source_id: PinnedSnapshot(version=2, snapshot=pg.next_version())
        }

        stale = Staleness.compute(snapshot, pinned, latest)
        by_target = {(s.target.kind, s.target.id, s.reason): s for s in stale.entries}

        removed = by_target[
            (EntityKind.NODE, process.customers.id, StaleReason.OBJECT_REMOVED)
        ]
        assert removed.since_version == 2
        assert removed.pinned_version == 1

        changed = by_target[
            (EntityKind.NODE, process.orders.id, StaleReason.OBJECT_CHANGED)
        ]
        assert changed.detail["column amount"] == "modified"
        assert changed.detail["column note"] == "added"

        column = by_target[
            (EntityKind.FLOW, process.flow_orders.id, StaleReason.COLUMN_CHANGED)
        ]
        assert column.detail["column"] == "amount"
        assert column.detail["type"] == "numeric(10,2) -> numeric(12,2)"

        routine = by_target[
            (EntityKind.FLOW, process.flow_orders.id, StaleReason.ROUTINE_CHANGED)
        ]
        assert "body" in routine.detail

        assert list(stale.of_target(EntityRef.of(process.v_orders))) == []
        assert Staleness.compute(snapshot, pinned, pinned).entries == ()
