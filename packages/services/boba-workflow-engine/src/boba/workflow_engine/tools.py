"""Tools workflow_save / workflow_run / workflow_list: workflow из чата.

Инструменты уровня приложения: не в песочнице, зовут WorkflowService под
контекстом текущего хода. Запуск ждёт завершения и возвращает модели итоги
всех задач; Stop хода останавливает и запуск.

Ошибки: ErrorResult — спека негодна, workflow не найден; остальное
упаковывает ToolErrorGuard.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field

from boba.identity.context import CallContext
from boba.identity.errors import RefusalError
from boba.toolkit.calls import ScriptCall, ToolCallViews
from boba.toolkit.result import (
    ErrorResult,
    TextResult,
    ToolResult,
    pack_result,
)
from boba.workflow.report import RunReport, WorkflowListing, WorkflowPrompt
from boba.workflow_engine.service import WorkflowService

__all__ = ["WorkflowPrompt", "WorkflowToolConfig", "build_workflow_tools"]

ServiceSource = Callable[[], Awaitable[WorkflowService]]


class WorkflowToolConfig(BaseModel):
    """Секция [tool.workflow]: у инструментов своих параметров нет."""

    model_config = ConfigDict(extra="ignore")


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
