"""Адаптер TaskExecutor к инструментам сессии; таблицей задач управляет TaskWorker.

Ошибки:
TaskExecutionError — вызов отвергнут или инструмент сообщил определённый отказ.
Exception — неизвестный исход внешнего исполнения; worker фиксирует interrupted.
"""

from __future__ import annotations

from boba.agent import models as m
from boba.agent.queries import Query
from boba.agent.runtime import Agent
from boba.agent.tasks import AgentTaskKind, ToolTaskInput, ToolTaskOutput
from boba.tasks.models import (
    ExecutionContext,
    TaskError,
    TaskExecutionError,
    TaskExecutor,
    TaskOutcome,
    TaskState,
    TaskStatus,
)


class StoredToolInvocation(m.DataModel):
    call: m.ToolCallRecord
    assistant: m.AssistantMessageRecord


class SessionToolExecutor(TaskExecutor):
    """Исполнитель заданий маршрута сессии, создаваемый SessionHub.

    Для вызова LLM восстанавливает источник и передаёт действие ToolRunner;
    задание планировщика без source передаёт внешнему входу ToolRunner.
    """

    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    async def execute(self, task: TaskState, context: ExecutionContext) -> TaskOutcome:
        coordinator = self.agent.tasks
        if coordinator is None:
            raise TaskExecutionError(
                TaskError(
                    code="missing_task_service",
                    message="Session has no task coordinator",
                )
            )

        if task.route_key != coordinator.route_key:
            raise TaskExecutionError(
                TaskError(
                    code="invalid_route",
                    message=f"Task {task.task_id} does not address session {self.agent.session_id}",
                )
            )

        if task.owner_id != coordinator.owner_id:
            raise TaskExecutionError(
                TaskError(
                    code="invalid_owner", message="Task owner does not own this session"
                )
            )

        if task.kind != AgentTaskKind.TOOL:
            raise TaskExecutionError(
                TaskError(
                    code="unsupported_task",
                    message=f"Unsupported task kind: {task.kind}",
                )
            )

        try:
            request = ToolTaskInput.model_validate(task.payload)
            if request.name.startswith("mcp__"):
                await self.agent.tools.wait_available(self.agent, request.name)

            if request.source is None:
                content = await self.agent.tools.external(self.agent, request)
            else:
                rows = await self.agent.store.rows(
                    Query.TOOL_EXECUTION,
                    self.agent.session_id,
                    request.source.call_record_id,
                )
                if not rows:
                    raise ValueError(
                        f"Tool source {request.source.call_record_id} is unavailable"
                    )

                source = StoredToolInvocation.model_validate(rows[0])
                if source.call.payload.task_id != task.task_id:
                    raise ValueError("Stored call refers to another task")

                if source.call.payload.name != request.name:
                    raise ValueError("Task name differs from its stored call")

                if source.call.payload.arguments != request.arguments:
                    raise ValueError("Task arguments differ from their stored call")

                content = await self.agent.tools.call(
                    self.agent, source.assistant, source.call
                )

            if isinstance(content, ToolTaskOutput):
                output = content
            else:
                output = ToolTaskOutput(content=content)

            return TaskOutcome(
                status=TaskStatus.COMPLETED, result=output.model_dump(mode="json")
            )

        except (ValueError, LookupError, PermissionError) as exc:
            raise TaskExecutionError(
                TaskError(code="tool_rejected", message=str(exc))
            ) from exc
