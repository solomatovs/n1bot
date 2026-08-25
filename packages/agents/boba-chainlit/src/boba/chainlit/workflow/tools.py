"""Tools workflow_save / workflow_run / workflow_list: workflow из чата.

Инструменты уровня приложения: не в песочнице, зовут WorkflowService под
контекстом текущего хода. Запуск ждёт завершения и возвращает модели итоги
всех задач; Stop хода останавливает и запуск.

Ошибки: ErrorResult — спека негодна, workflow не найден; остальное
упаковывает ToolErrorGuard.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Annotated

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit.domain.context import CallContext
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.workflow.service import RunOutcome, WorkflowService
from boba.chainlit.workflow.store import StoredWorkflow
from boba.toolkit.calls import ScriptCall, ToolCallViews
from boba.toolkit.result import (
    ErrorResult,
    MultiResult,
    TextResult,
    ToolResult,
    pack_result,
)
from boba.workflow import TaskStatus

__all__ = ["WorkflowPrompt", "WorkflowToolConfig", "build_workflow_tools"]

ServiceSource = Callable[[], Awaitable[WorkflowService]]


class WorkflowToolConfig(BaseModel):
    """Секция [tool.workflow]: у инструментов своих параметров нет."""

    model_config = ConfigDict(extra="ignore")


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


def build_workflow_tools(
    cfg: WorkflowToolConfig, service: ServiceSource
) -> list[BaseTool]:
    # спека — yaml: шаг ленты показывает её кодом, а не json-аргументами
    ToolCallViews.register("workflow_save", ScriptCall(arg="spec", lang="yaml"))

    @tool(response_format="content_and_artifact")
    async def workflow_save(
        spec: Annotated[str, Field(min_length=1, description=WorkflowPrompt.SPEC)],
    ) -> tuple[str, ToolResult]:
        """Сохранить workflow: проверить спеку и записать определение."""
        context = CallContext.current()
        try:
            stored = await (await service()).save(context.subject, spec, {})
        except RefusalError as e:
            return pack_result(ErrorResult(message=str(e), error_kind=e.kind))

        text = f"workflow {stored.name!r} saved (id {stored.id}); tools: " + ", ".join(
            stored.tools
        )
        return pack_result(TextResult(text=text))

    @tool(response_format="content_and_artifact")
    async def workflow_run(
        name: Annotated[str, Field(min_length=1, description=WorkflowPrompt.NAME)],
    ) -> tuple[str, ToolResult]:
        """Запустить сохранённый workflow и дождаться итогов всех задач."""
        context = CallContext.current()
        try:
            resolved = await service()
            stored = await resolved.get_by_name(context.subject, name)
            outcome = await resolved.run(context, stored, resolved.new_run_id())
        except RefusalError as e:
            return pack_result(ErrorResult(message=str(e), error_kind=e.kind))

        return pack_result(RunReport.of(outcome))

    @tool(response_format="content_and_artifact")
    async def workflow_list() -> tuple[str, ToolResult]:
        """Перечислить сохранённые workflow."""
        context = CallContext.current()
        stored = await (await service()).list_workflows(context.subject)

        return pack_result(TextResult(text=WorkflowListing.render(stored)))

    workflow_save.description = str(WorkflowPrompt.SAVE)
    workflow_run.description = str(WorkflowPrompt.RUN)
    workflow_list.description = str(WorkflowPrompt.LIST)

    return [workflow_save, workflow_run, workflow_list]
