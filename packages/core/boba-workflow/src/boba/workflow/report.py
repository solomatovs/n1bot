"""Отчёт о запуске и тексты фасада workflow для модели."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from boba.toolkit.result import MultiResult, TextResult, ToolResult
from boba.workflow.graph import TaskStatus
from boba.workflow.records import RunOutcome, StoredWorkflow

__all__ = ["ReportKey", "RunReport", "WorkflowListing", "WorkflowPrompt"]


class WorkflowPrompt(StrEnum):
    """Тексты фасада для модели."""

    SPEC = (
        "YAML workflow: name, description, tasks{name: {tool, args}}, edges. "
        "Edge kinds: 'a -> b' (b after a), 'a.result -> b.args.q' (result of a "
        "substituted into argument q of b; a set argument must mention "
        "'{{ a }}'), '[b, c] -> d' (d after both). Tools and their arguments "
        "are the ones available to you; chat-only tools cannot be used. A task "
        "may set 'intent' in args (a one-line caption); otherwise it is derived."
    )
    NAME = "Name of a saved workflow, as returned by workflow_save or workflow_list."
    SAVE = (
        "Save a workflow definition for later runs. The spec is validated "
        "against the tools available to you; on error fix the spec and retry."
    )
    RUN = (
        "Run a saved workflow and wait for it: tasks run in parallel where "
        "the graph allows, dependants of a failed task are skipped. Returns "
        "the results of every task."
    )
    LIST = "List the saved workflows with their tools."


class ReportKey(StrEnum):
    """Ключи metadata отчёта о запуске."""

    RUN_ID = "run_id"
    STATUS = "status"
    TASKS = "tasks"
    TASK = "task"


class RunReport:
    """Итог запуска для модели: статус каждой задачи и результаты по порядку."""

    @classmethod
    def of(cls, outcome: RunOutcome) -> ToolResult:
        """Первый элемент — сводка по задачам, дальше результаты в порядке спеки."""
        items: list[ToolResult] = []
        marks: list[str] = []
        for name, task in outcome.state.tasks.items():
            marks.append(f"{name}={task.status.value}")
            result = outcome.results.get(name)
            if result is None:
                continue

            items.append(cls._labelled(name, task.status, result))

        summary = TextResult(text=cls._summary(outcome))
        return MultiResult(
            ok=outcome.state.ok,
            items=[summary, *items],
            metadata={
                ReportKey.RUN_ID: str(outcome.run.id),
                ReportKey.STATUS: outcome.state.status.value,
                ReportKey.TASKS: ", ".join(marks),
            },
        )

    @staticmethod
    def _summary(outcome: RunOutcome) -> str:
        lines = [f"workflow run {outcome.run.id}: {outcome.state.status.value}"]
        for name, task in outcome.state.tasks.items():
            line = f"- {name}: {task.status.value}"
            if task.error:
                line = f"{line}: {task.error}"

            lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _labelled(name: str, status: TaskStatus, result: ToolResult) -> ToolResult:
        """Результат задачи с её именем и статусом в metadata."""
        metadata = dict(result.metadata)
        metadata[ReportKey.TASK] = name
        metadata[ReportKey.STATUS] = status.value
        return result.model_copy(update={"metadata": metadata})


class WorkflowListing:
    """Список сохранённых workflow текстом."""

    @staticmethod
    def render(stored: Sequence[StoredWorkflow]) -> str:
        if not stored:
            return "no saved workflows"

        lines: list[str] = []
        for item in stored:
            tools = ", ".join(item.tools)
            lines.append(f"- {item.name} (id {item.id}): tools {tools}")

        return "\n".join(lines)
