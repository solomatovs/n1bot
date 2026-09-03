"""REST workflow для страницы: определения, запуск и остановка; каталог — /v1/tools.

Пользователь — из cookie входа, профиль — в теле либо ?profile= (без него
берётся профиль по умолчанию). Запуск идёт в фоне процесса, ответ — id
запуска; ход виден по GET записи, остановка — POST stop на этом инстансе.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — профиль недоступен ролям пользователя.
400 — спека негодна или содержит недоступные инструменты.
404 — workflow или запуск не пользователя.
202 — запуск ведёт другой инстанс: команда остановки принята шиной.
503 — хранилище workflow недоступно.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, TypeVar
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from boba.chat.profiles import ChatProfiles
from boba.identity.api import ApiSubject, AuthenticatedUser
from boba.identity.context import Scope
from boba.studio.api.auth import ApiAuth, CurrentSubject, CurrentUser
from boba.studio.api.urls import WorkflowUrl
from boba.workflow import RunState
from boba.workflow.records import (
    StoredRun,
    StoredWorkflow,
    WorkflowStoreError,
)
from boba.workflow_engine.service import (
    StopOutcome,
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


class DraftBody(BaseModel):
    """Черновик билдера к записи: спека как есть, раскладка и сокет вкладки-автора."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str | None = None
    spec: str
    layout: Mapping[str, Any] = Field(default_factory=dict)
    sid: str = ""


class ProfileBody(BaseModel):
    """Действие без данных: только профиль."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str | None = None


class RunStarted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID


class Stopped(BaseModel):
    """Итог просьбы остановить: stopped — остановлен здесь, accepted — команда
    принята для другого инстанса, finished — уже завершён.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: StopOutcome


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
            (WorkflowUrl.WORKFLOW, self.save_into, "PUT"),
            (WorkflowUrl.WORKFLOW, self.delete, "DELETE"),
            (WorkflowUrl.WORKFLOW_DRAFT, self.put_draft, "PUT"),
            (WorkflowUrl.WORKFLOW_DRAFT, self.clear_draft, "DELETE"),
            (WorkflowUrl.RUN, self.run, "POST"),
            (WorkflowUrl.RUNS, self.list_runs, "GET"),
            (WorkflowUrl.RUN_ONE, self.get_run, "GET"),
            (WorkflowUrl.STOP, self.stop, "POST"),
        )
        for path, handler, method in routes:
            router.add_api_route(path.value, handler, methods=[method], tags=[self.TAG])

    async def validate(
        self, body: WorkflowBody, current_user: CurrentUser, profile: str | None = None
    ) -> RunState:
        identity = self._identity(current_user, self._chosen(body.profile, profile))
        service = await self._resolved()

        try:
            graph = await service.validate(identity.subject, body.spec)
        except WorkflowError as exc:
            raise self._http(exc) from exc

        return service.initial_state(graph)

    async def list_workflows(
        self, identity: CurrentSubject
    ) -> Sequence[StoredWorkflow]:
        service = await self._resolved()

        return await self._guarded(service.list_workflows(identity.subject))

    async def save(
        self, body: WorkflowBody, current_user: CurrentUser, profile: str | None = None
    ) -> StoredWorkflow:
        identity = self._identity(current_user, self._chosen(body.profile, profile))
        service = await self._resolved()

        return await self._guarded(
            service.save(identity.subject, body.spec, body.layout)
        )

    async def save_into(
        self,
        workflow_id: UUID,
        body: WorkflowBody,
        current_user: CurrentUser,
        profile: str | None = None,
    ) -> StoredWorkflow:
        identity = self._identity(current_user, self._chosen(body.profile, profile))
        service = await self._resolved()

        return await self._guarded(
            service.save_into(identity.subject, workflow_id, body.spec, body.layout)
        )

    async def get(self, workflow_id: UUID, identity: CurrentSubject) -> StoredWorkflow:
        service = await self._resolved()

        return await self._guarded(service.get(identity.subject, workflow_id))

    async def delete(self, workflow_id: UUID, identity: CurrentSubject) -> Deleted:
        service = await self._resolved()

        deleted = await self._guarded(service.delete(identity.subject, workflow_id))
        return Deleted(deleted=deleted)

    async def put_draft(
        self,
        workflow_id: UUID,
        body: DraftBody,
        current_user: CurrentUser,
        profile: str | None = None,
    ) -> StoredWorkflow:
        identity = self._identity(current_user, self._chosen(body.profile, profile))
        service = await self._resolved()

        return await self._guarded(
            service.put_draft(
                identity.subject, workflow_id, body.spec, body.layout, body.sid
            )
        )

    async def clear_draft(
        self,
        workflow_id: UUID,
        identity: CurrentSubject,
        sid: str = "",
    ) -> StoredWorkflow:
        service = await self._resolved()

        return await self._guarded(
            service.clear_draft(identity.subject, workflow_id, sid)
        )

    async def run(
        self,
        workflow_id: UUID,
        body: ProfileBody,
        current_user: CurrentUser,
        profile: str | None = None,
    ) -> RunStarted:
        identity = self._identity(current_user, self._chosen(body.profile, profile))
        service = await self._resolved()

        run_id = service.new_run_id()
        context = identity.context(Scope.workflow(run_id))
        stored = await self._guarded(service.get(identity.subject, workflow_id))
        started = await self._guarded(service.start(context, stored, run_id))

        service.launch(context, started)

        return RunStarted(run_id=run_id)

    async def list_runs(
        self,
        identity: CurrentSubject,
        limit: int = 50,
    ) -> Sequence[StoredRun]:
        service = await self._resolved()

        return await self._guarded(service.list_runs(identity.subject, limit))

    async def get_run(self, run_id: UUID, identity: CurrentSubject) -> StoredRun:
        service = await self._resolved()

        return await self._guarded(service.get_run(identity.subject, run_id))

    async def stop(
        self,
        run_id: UUID,
        body: ProfileBody,
        current_user: CurrentUser,
        response: Response,
        profile: str | None = None,
    ) -> Stopped:
        identity = self._identity(current_user, self._chosen(body.profile, profile))
        service = await self._resolved()

        outcome = await self._guarded(service.stop(identity.subject, run_id))
        if outcome is StopOutcome.ACCEPTED:
            response.status_code = 202

        return Stopped(outcome=outcome)

    @staticmethod
    def _chosen(in_body: str | None, in_query: str | None) -> str | None:
        """Профиль из тела главнее query-параметра, которым страница метит запросы."""
        if in_body is not None:
            return in_body

        return in_query

    def _identity(
        self, current_user: AuthenticatedUser, profile: str | None
    ) -> ApiSubject:
        return ApiAuth.resolve(current_user, profile, self._profiles)

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

        return HTTPException(status_code=400, detail=str(exc))
