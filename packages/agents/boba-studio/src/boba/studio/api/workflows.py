"""REST workflow для страницы: определения, запуск и остановка; каталог — /v1/tools.

Пользователь — из cookie входа, профиль — в теле или query (без него
берётся единственный видимый). Запуск идёт в фоне процесса, ответ — id
запуска; ход виден по GET записи, остановка — POST stop на этом инстансе.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — профиль недоступен ролям пользователя.
400 — спека негодна или содержит недоступные инструменты.
404 — workflow или запуск не пользователя.
409 — запуск исполняет другой инстанс: остановить можно только там.
503 — хранилище workflow недоступно.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, TypeVar
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boba.chat.profiles import ChatProfiles
from boba.identity.api import ApiSubject, AuthenticatedUser
from boba.identity.context import Scope
from boba.studio.api.auth import ApiIdentity, CurrentUser
from boba.studio.api.urls import WorkflowUrl
from boba.workflow import RunState
from boba.workflow.records import StoredRun, StoredWorkflow, WorkflowStoreError
from boba.workflow_engine.service import (
    WorkflowError,
    WorkflowRefusal,
    WorkflowService,
)

__all__ = ["WorkflowApi", "WorkflowBody"]

logger = logging.getLogger(__name__)

ServiceSource = Callable[[], Awaitable[WorkflowService]]

T = TypeVar("T")


class WorkflowBody(BaseModel):
    """Определение к сохранению или проверке."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str | None = None
    spec: str = Field(min_length=1)
    layout: Mapping[str, Any] = Field(default_factory=dict)


class ProfileBody(BaseModel):
    """Действие без данных: только профиль."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str | None = None


class RunStarted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID


class Stopped(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stopped: bool


class Deleted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    deleted: bool


class WorkflowApi:
    """Обработчики REST workflow."""

    TAG: ClassVar[str] = "workflows"

    def __init__(self, service: ServiceSource, profiles: ChatProfiles) -> None:
        self._service = service
        self._profiles = profiles

    def mount(self, router: APIRouter) -> None:
        routes = (
            (WorkflowUrl.VALIDATE, self.validate, "POST"),
            (WorkflowUrl.WORKFLOWS, self.list_workflows, "GET"),
            (WorkflowUrl.WORKFLOWS, self.save, "POST"),
            (WorkflowUrl.WORKFLOW, self.get, "GET"),
            (WorkflowUrl.WORKFLOW, self.delete, "DELETE"),
            (WorkflowUrl.RUN, self.run, "POST"),
            (WorkflowUrl.RUNS, self.list_runs, "GET"),
            (WorkflowUrl.RUN_ONE, self.get_run, "GET"),
            (WorkflowUrl.STOP, self.stop, "POST"),
        )
        for path, handler, method in routes:
            router.add_api_route(path.value, handler, methods=[method], tags=[self.TAG])

    async def validate(self, body: WorkflowBody, current_user: CurrentUser) -> RunState:
        identity = self._identity(current_user, body.profile)
        service = await self._resolved()

        try:
            graph = await service.validate(identity.subject, body.spec)
        except WorkflowError as exc:
            raise self._http(exc) from exc

        return service.initial_state(graph)

    async def list_workflows(
        self, current_user: CurrentUser, profile: str | None = None
    ) -> Sequence[StoredWorkflow]:
        identity = self._identity(current_user, profile)
        service = await self._resolved()

        return await self._guarded(service.list_workflows(identity.subject))

    async def save(
        self, body: WorkflowBody, current_user: CurrentUser
    ) -> StoredWorkflow:
        identity = self._identity(current_user, body.profile)
        service = await self._resolved()

        return await self._guarded(
            service.save(identity.subject, body.spec, body.layout)
        )

    async def get(
        self, workflow_id: int, current_user: CurrentUser, profile: str | None = None
    ) -> StoredWorkflow:
        identity = self._identity(current_user, profile)
        service = await self._resolved()

        return await self._guarded(service.get(identity.subject, workflow_id))

    async def delete(
        self, workflow_id: int, current_user: CurrentUser, profile: str | None = None
    ) -> Deleted:
        identity = self._identity(current_user, profile)
        service = await self._resolved()

        deleted = await self._guarded(service.delete(identity.subject, workflow_id))
        return Deleted(deleted=deleted)

    async def run(
        self, workflow_id: int, body: ProfileBody, current_user: CurrentUser
    ) -> RunStarted:
        identity = self._identity(current_user, body.profile)
        service = await self._resolved()

        run_id = service.new_run_id()
        context = identity.context(Scope.workflow(run_id))
        stored = await self._guarded(service.get(identity.subject, workflow_id))
        started = await self._guarded(service.start(context, stored, run_id))

        service.launch(context, started)

        return RunStarted(run_id=run_id)

    async def list_runs(
        self,
        current_user: CurrentUser,
        profile: str | None = None,
        limit: int = 50,
    ) -> Sequence[StoredRun]:
        identity = self._identity(current_user, profile)
        service = await self._resolved()

        return await self._guarded(service.list_runs(identity.subject, limit))

    async def get_run(
        self, run_id: UUID, current_user: CurrentUser, profile: str | None = None
    ) -> StoredRun:
        identity = self._identity(current_user, profile)
        service = await self._resolved()

        return await self._guarded(service.get_run(identity.subject, run_id))

    async def stop(
        self, run_id: UUID, body: ProfileBody, current_user: CurrentUser
    ) -> Stopped:
        identity = self._identity(current_user, body.profile)
        service = await self._resolved()

        stopped = await self._guarded(service.stop(identity.subject, run_id))
        return Stopped(stopped=stopped)

    def _identity(
        self, current_user: AuthenticatedUser | None, profile: str | None
    ) -> ApiSubject:
        return ApiIdentity.resolve(current_user, profile, self._profiles)

    async def _resolved(self) -> WorkflowService:
        try:
            return await self._service()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @staticmethod
    async def _guarded(action: Awaitable[T]) -> T:
        """Отказы сервиса и хранилища — в HTTP-статусы."""
        try:
            return await action
        except WorkflowError as exc:
            raise WorkflowApi._http(exc) from exc
        except WorkflowStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @staticmethod
    def _http(exc: WorkflowError) -> HTTPException:
        if exc.kind == WorkflowRefusal.NOT_FOUND:
            return HTTPException(status_code=404, detail=str(exc))

        if exc.kind == WorkflowRefusal.OTHER_INSTANCE:
            return HTTPException(status_code=409, detail=str(exc))

        return HTTPException(status_code=400, detail=str(exc))
