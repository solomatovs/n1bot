"""Кадры синхронизации: снимок режется на порции и собирается обратно
байт-в-байт; порядок порций, счётчики порций и итога проверяются."""

from __future__ import annotations

import pytest

from boba.catalog import (
    SnapshotAssembler,
    SnapshotBatches,
    SourceKinds,
    SyncBatch,
    SyncDone,
    SyncFrameError,
    SyncPlan,
)
from boba.db.clickhouse.snapshot import ChSnapshot, ChSourceKind
from boba.db.postgres.snapshot import (
    PgPart,
    PgSnapshot,
    PgSourceKind,
)
from boba.db.postgres.snapshot_sample import PgSample

BATCH = 2
KINDS = SourceKinds.of(PgSnapshot, ChSnapshot)


def _plan(snapshot: PgSnapshot) -> SyncPlan:
    objects = (
        len(snapshot.relations)
        + len(snapshot.routines)
        + len(snapshot.sequences)
        + len(snapshot.types)
    )
    return SyncPlan(
        source_kind=PgSourceKind.POSTGRES.value,
        database="prod",
        schemas=("public", "etl"),
        objects_total=objects,
    )


def _batches(snapshot: PgSnapshot) -> list[tuple[SyncBatch, bytes]]:
    """Каждая часть порциями по BATCH записей; объекты считаются у отношений,
    рутин, последовательностей и типов."""
    counted = {PgPart.RELATIONS, PgPart.ROUTINES, PgPart.SEQUENCES, PgPart.TYPES}
    frames: list[tuple[SyncBatch, bytes]] = []
    seq = 0
    for part in PgPart:
        records = list(getattr(snapshot, part.value))
        for start in range(0, len(records), BATCH):
            chunk = records[start : start + BATCH]
            seq += 1
            objects = 0
            if part in counted:
                objects = len(chunk)

            head = SyncBatch(
                seq=seq, part=part.value, count=len(chunk), objects=objects
            )
            frames.append((head, SnapshotBatches.encode(chunk)))

    return frames


def _done(snapshot: PgSnapshot, batches: int) -> SyncDone:
    counts: dict[str, int] = {}
    for part in PgPart:
        counts[part.value] = len(getattr(snapshot, part.value))

    return SyncDone(
        counts=counts, objects_total=_plan(snapshot).objects_total, batches=batches
    )


def test_snapshot_survives_the_frame_round_trip() -> None:
    snapshot = PgSample().snapshot()
    assembler = SnapshotAssembler(_plan(snapshot), KINDS)

    frames = _batches(snapshot)
    for head, body in frames:
        assembler.take(head, body)

    built = assembler.finish(_done(snapshot, len(frames)))
    assert built == snapshot
    assert assembler.objects_done == _plan(snapshot).objects_total


def test_out_of_order_batch_and_wrong_count_are_refused() -> None:
    snapshot = PgSample().snapshot()
    assembler = SnapshotAssembler(_plan(snapshot), KINDS)
    head, body = _batches(snapshot)[0]

    with pytest.raises(SyncFrameError, match="came out of order, expected #1"):
        assembler.take(head.model_copy(update={"seq": 2}), body)

    with pytest.raises(SyncFrameError, match="declares 5 records, the body holds"):
        assembler.take(head.model_copy(update={"count": 5}), body)

    with pytest.raises(SyncFrameError, match="is not a JSON list of PgDatabase"):
        assembler.take(head, b'[{"nope": 1}]')

    with pytest.raises(SyncFrameError, match="has no part 'nope'"):
        assembler.take(head.model_copy(update={"part": "nope"}), body)


def test_done_must_match_the_batches_and_the_plan() -> None:
    snapshot = PgSample().snapshot()
    assembler = SnapshotAssembler(_plan(snapshot), KINDS)
    frames = _batches(snapshot)
    for head, body in frames[:-1]:
        assembler.take(head, body)

    with pytest.raises(
        SyncFrameError, match="declares 1 records of part 'types', 0 arrived"
    ):
        assembler.finish(_done(snapshot, len(frames)))

    assembler.take(*frames[-1])
    short = _done(snapshot, len(frames)).model_copy(update={"objects_total": 1})
    with pytest.raises(SyncFrameError, match="reports 1 objects, the plan promised"):
        assembler.finish(short)


def test_assembler_builds_the_snapshot_class_of_the_plan_kind() -> None:
    plan = SyncPlan(
        source_kind=ChSourceKind.CLICKHOUSE.value,
        database="dwh",
        schemas=(),
        objects_total=0,
    )
    assembler = SnapshotAssembler(plan, KINDS)
    assert assembler.part("tables").model.__name__ == "ChTable"
    with pytest.raises(SyncFrameError, match="has no part 'relations'"):
        assembler.part("relations")
