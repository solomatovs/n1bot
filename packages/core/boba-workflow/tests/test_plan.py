"""Автомат запуска: готовность стадий, отказ, стоп, протокол."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from catalog import catalog
from test_spec import EXAMPLE

from boba.workflow import (
    RunStatus,
    Stage,
    TaskStatus,
    WorkflowGraph,
    WorkflowPlan,
    WorkflowPlanError,
    WorkflowSpec,
)

T0 = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def plan() -> WorkflowPlan:
    return WorkflowPlan(
        WorkflowGraph.build(WorkflowSpec.parse_yaml(EXAMPLE), catalog())
    )


def tasks_of(stages: tuple[Stage, ...]) -> set[str]:
    names: set[str] = set()
    for stage in stages:
        names.update(stage.tasks)

    return names


def run_stage(
    plan: WorkflowPlan, stage: Stage, status: TaskStatus = TaskStatus.DONE
) -> None:
    for task in stage.tasks:
        plan.started(task, f"call-{task}", T0)

    for task in stage.tasks:
        plan.finished(task, status, T0 + timedelta(seconds=2))


def test_happy_path() -> None:
    p = plan()
    assert p.snapshot().status is RunStatus.PENDING

    first = p.ready()
    assert tasks_of(first) == {"dump", "load", "count", "ids"}
    assert p.ready() == ()
    assert p.snapshot().status is RunStatus.RUNNING

    for stage in first:
        run_stage(p, stage)

    second = p.ready()
    assert tasks_of(second) == {"check"}
    run_stage(p, second[0])

    assert p.done
    state = p.snapshot()
    assert state.status is RunStatus.DONE
    assert state.ok
    assert state.tasks["check"].call_id == "call-check"
    assert state.tasks["check"].elapsed_ms == 2000


def test_failed_dependency_skips_dependents_only() -> None:
    p = plan()
    first = p.ready()
    by_tasks = {stage.tasks: stage for stage in first}

    run_stage(p, by_tasks[("ids",)], TaskStatus.FAILED)
    assert p.ready() == ()

    run_stage(p, by_tasks[("count", "dump", "load")])
    assert p.ready() == ()

    assert p.done
    state = p.snapshot()
    assert state.status is RunStatus.FAILED
    assert state.tasks["check"].status is TaskStatus.SKIPPED
    assert state.tasks["ids"].status is TaskStatus.FAILED
    assert state.tasks["dump"].status is TaskStatus.DONE


def test_stop_halts_pending_and_waits_for_running() -> None:
    p = plan()
    first = p.ready()

    p.started("ids", "call-ids", T0)
    p.stop()

    state = p.snapshot()
    assert state.tasks["dump"].status is TaskStatus.STOPPED
    assert state.tasks["ids"].status is TaskStatus.RUNNING
    assert not p.done

    p.finished("ids", TaskStatus.STOPPED, T0)
    assert p.ready() == ()

    assert p.done
    state = p.snapshot()
    assert state.status is RunStatus.STOPPED
    assert state.tasks["check"].status is TaskStatus.STOPPED
    assert len(first) == 2


def test_protocol_violations() -> None:
    p = plan()
    p.ready()

    with pytest.raises(WorkflowPlanError):
        p.finished("ids", TaskStatus.DONE, T0)

    p.started("ids", "c", T0)
    with pytest.raises(WorkflowPlanError):
        p.started("ids", "c", T0)

    with pytest.raises(WorkflowPlanError):
        p.finished("ids", TaskStatus.SKIPPED, T0)

    with pytest.raises(WorkflowPlanError):
        p.started("nope", "c", T0)
