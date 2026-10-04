"""REST workflow отключён: каждый маршрут отвечает 501.

Движок запуска workflow удалён. Маршруты оставлены, чтобы страница получала
внятный отказ, а не 404; запуск вне чата — POST /v1/tools/{name} и
POST /v1/dags/runs.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
501 — запуск workflow отключён.
"""

from __future__ import annotations

from typing import ClassVar

from fastapi import APIRouter, HTTPException

from boba.studio.api.auth import CurrentUser
from boba.studio.api.urls import WorkflowUrl

__all__ = ["WorkflowApi"]


class WorkflowApi:
    """Заглушка маршрутов /workflows и /workflow-runs: все отвечают 501."""

    TAG: ClassVar[str] = "workflows"

    DISABLED: ClassVar[str] = (
        "workflow runs are disabled: the workflow engine was removed; call a "
        "tool with POST /v1/tools/{name} or run a DAG with POST /v1/dags/runs"
    )

    ROUTES: ClassVar[tuple[tuple[WorkflowUrl, str], ...]] = (
        (WorkflowUrl.VALIDATE, "POST"),
        (WorkflowUrl.WORKFLOWS, "GET"),
        (WorkflowUrl.WORKFLOWS, "POST"),
        (WorkflowUrl.WORKFLOW, "GET"),
        (WorkflowUrl.WORKFLOW, "PUT"),
        (WorkflowUrl.WORKFLOW, "DELETE"),
        (WorkflowUrl.WORKFLOW_DRAFT, "PUT"),
        (WorkflowUrl.WORKFLOW_DRAFT, "DELETE"),
        (WorkflowUrl.RUN, "POST"),
        (WorkflowUrl.RUNS, "GET"),
        (WorkflowUrl.RUN_ONE, "GET"),
        (WorkflowUrl.STOP, "POST"),
        (WorkflowUrl.STREAM, "GET"),
        (WorkflowUrl.STREAM_CHANNELS, "GET"),
    )

    def mount(self, router: APIRouter) -> None:
        for path, method in self.ROUTES:
            router.add_api_route(
                path.value, self.disabled, methods=[method], tags=[self.TAG]
            )

    async def disabled(self, current_user: CurrentUser) -> None:
        raise HTTPException(status_code=501, detail=self.DISABLED)
