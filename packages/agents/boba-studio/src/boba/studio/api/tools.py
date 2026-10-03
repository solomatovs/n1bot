"""Каталог инструментов субъекта и REST-запуск одного человеком.

GET /v1/tools — каталог страницы сборки: отключён, отвечает 501.
POST /v1/tools/{name}: тред, профиль, intent и аргументы в теле, пользователь —
из cookie входа. Контекст вызова собирается здесь под HumanInitiator(api);
видимость инструментов — headless-решение ToolAccess, инструменты чата
недоступны.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — профиль недоступен ролям пользователя.
404 — тред не пользователя, инструмент не собран, не разрешён или только
    для чата.
503 — слой данных недоступен.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, ClassVar
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boba.chat.profiles import ChatProfiles
from boba.identity.context import (
    CallContext,
    CallContexts,
    Scope,
    Subject,
)
from boba.identity.locks import (
    LiveLocks,
    LockBusyError,
    LockKeeper,
    LockMode,
    LockPurpose,
)
from boba.identity.run import RunRegistry
from boba.studio.api.auth import ApiAuth, CurrentUser
from boba.studio.api.urls import ToolCallUrl
from boba.toolkit.calls import ToolIntent
from boba.toolrun.dag_run import NodeOutcome
from boba.toolrun.invoke import (
    CallIdPrefix,
    ToolInvoker,
    ToolUnavailableError,
)
from boba.toolrun.registry import ToolRegistry

__all__ = ["JobLock", "ToolCallBody", "ToolCallReply", "ToolCalling"]

logger = logging.getLogger(__name__)

RegistrySource = Callable[[], Awaitable[ToolRegistry]]
"""Реестр инструментов приложения; собирается контейнером на первый запрос."""

"""Владение тредами: реализует слой данных хоста, зовётся на вызов."""

LocksSource = Callable[[], LiveLocks]
"""Блокировки областей процесса, зовётся на вызов."""


class ToolCallBody(BaseModel):
    """Тело запроса: где и от какого профиля запускать, с чем."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str = Field(min_length=1)
    intent: str = Field(min_length=1, max_length=ToolIntent.MAX_CHARS)
    args: Mapping[str, Any] = Field(default_factory=dict)


class ToolCallReply(BaseModel):
    """Итог вызова: текст для читателя и результат инструмента как есть."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    ok: bool
    content: str
    result: Mapping[str, Any]

    @classmethod
    def of(cls, reply: NodeOutcome, call_id: str) -> ToolCallReply:
        return cls(
            call_id=call_id,
            ok=reply.ok(),
            content=reply.content,
            result=reply.artifact.model_dump(mode="json"),
        )


class JobLock:
    """Эксклюзивная блокировка области вызова через API на время работы.

    Область занята ходом или другим вызовом — 409 с именем держателя;
    пока вызов идёт, LockKeeper продлевает блокировку.
    """

    def __init__(self, locks: LocksSource, heartbeat_sec: float) -> None:
        self._locks = locks
        self._heartbeat_sec = heartbeat_sec

    @asynccontextmanager
    async def held(self, context: CallContext) -> AsyncGenerator[None, None]:
        locks = self._locks()
        try:
            lock = await locks.acquire(
                context.scope,
                LockMode.EXCLUSIVE,
                LockPurpose.TOOL_CALL,
                context.subject.user_id,
            )
        except LockBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        keeper = LockKeeper(locks, lock, context.cancellation, self._heartbeat_sec)
        async with keeper:
            yield


class ToolCalling:
    """Обработчик POST /tools/{name}."""

    TAG: ClassVar[str] = "tools"

    CATALOG_DISABLED: ClassVar[str] = (
        "the tool catalog of the workflow page is disabled: the workflow engine "
        "was removed; call a tool with POST /v1/tools/{name} or run a DAG with "
        "POST /v1/dags/runs"
    )

    def __init__(
        self,
        registry: RegistrySource,
        profiles: ChatProfiles,
        locks: LocksSource,
        heartbeat_sec: float,
        contexts: CallContexts,
    ) -> None:
        self._registry = registry
        self._profiles = profiles
        self._job_lock = JobLock(locks, heartbeat_sec)
        self._contexts = contexts

    def mount(self, router: APIRouter) -> None:
        router.add_api_route(
            ToolCallUrl.CATALOG.value,
            self.catalog,
            methods=["GET"],
            tags=[self.TAG],
        )
        router.add_api_route(
            ToolCallUrl.CALL.value, self.serve, methods=["POST"], tags=[self.TAG]
        )

    async def catalog(self, current_user: CurrentUser) -> None:
        """Каталог страницы сборки отключён вместе с запуском workflow."""
        raise HTTPException(status_code=501, detail=self.CATALOG_DISABLED)

    async def serve(
        self,
        name: str,
        body: ToolCallBody,
        current_user: CurrentUser,
    ) -> ToolCallReply:
        identity = ApiAuth.resolve(current_user, body.profile, self._profiles)

        # вызов живёт в своей области job: тредов чата у studio нет
        job_id = str(uuid4())

        invoker = await self._invoker(identity.subject)
        context = identity.context(Scope.job(job_id))

        async with self._job_lock.held(context):
            return await self._run(invoker, name, body, context)

    async def _invoker(self, subject: Subject) -> ToolInvoker:
        registry = await self._registry()

        return ToolInvoker.for_subject(registry, subject)

    async def _run(
        self, invoker: ToolInvoker, name: str, body: ToolCallBody, context: CallContext
    ) -> ToolCallReply:
        try:
            invoker.tool(name)
        except ToolUnavailableError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        call = invoker.call(name, body.args, body.intent, CallIdPrefix.API)
        call_id = str(call["id"])

        logger.info(
            "api tool call: %s by %s in thread %s",
            name,
            context.subject.login,
            context.scope.id,
        )

        with RunRegistry.open(self._contexts, context):
            reply = await invoker.invoke(call)

        return ToolCallReply.of(reply, call_id)
