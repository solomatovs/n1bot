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

from pydantic import BaseModel, ConfigDict, Field

from boba.identity.context import CallContext
from boba.identity.errors import RefusalError
from boba.toolkit.facade import PayloadTool, tool
from boba.toolkit.result import ErrorResult, MarkdownResult
from boba.workflow.report import RunReport, WorkflowListing, WorkflowPrompt
from boba.workflow_engine.service import WorkflowService

__all__ = ["WorkflowPrompt", "WorkflowToolConfig", "build_workflow_tools"]

ServiceSource = Callable[[], Awaitable[WorkflowService]]


class WorkflowToolConfig(BaseModel):
    """Секция [tool.workflow]: у инструментов своих параметров нет."""

    model_config = ConfigDict(extra="ignore")


def build_workflow_tools(
    cfg: WorkflowToolConfig, service: ServiceSource
) -> list[PayloadTool]:
    @tool
    async def workflow_save(
        spec: Annotated[
            str,
            Field(min_length=1, description=WorkflowPrompt.SPEC),
            MarkdownResult(language="yaml"),
        ],
    ) -> MarkdownResult | ErrorResult:
        """Сохранить workflow: проверить спеку и записать определение."""
        context = CallContext.current()
        try:
            stored = await (await service()).save(context.subject, spec, {})
        except RefusalError as e:
            return ErrorResult(message=str(e), error_kind=e.kind)

        tools = ", ".join(stored.tools)
        text = f"workflow {stored.name!r} saved (id {stored.id}); tools: {tools}"

        return MarkdownResult(text=text)

    @tool
    async def workflow_run(
        name: Annotated[str, Field(min_length=1, description=WorkflowPrompt.NAME)],
    ) -> MarkdownResult | ErrorResult:
        """Запустить сохранённый workflow и дождаться итогов всех задач."""
        context = CallContext.current()
        try:
            resolved = await service()
            stored = await resolved.get_by_name(context.subject, name)
            outcome = await resolved.run(context, stored, resolved.new_run_id())
        except RefusalError as e:
            return ErrorResult(message=str(e), error_kind=e.kind)

        return RunReport.of(outcome)

    @tool
    async def workflow_list() -> MarkdownResult:
        """Перечислить сохранённые workflow."""
        context = CallContext.current()
        stored = await (await service()).list_workflows(context.subject)

        return MarkdownResult(text=WorkflowListing.render(stored))

    workflow_save.description = str(WorkflowPrompt.SAVE)
    workflow_run.description = str(WorkflowPrompt.RUN)
    workflow_list.description = str(WorkflowPrompt.LIST)

    return [workflow_save, workflow_run, workflow_list]
