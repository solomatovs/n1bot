"""Каталог инструментов субъекта и REST-запуск одного человеком.

GET /v1/tools — каталог под роли и профиль (то, что видит страница workflow).
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
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boba.chat.profiles import ChatProfiles
from boba.chat.threads import DataRejectedError, DataUnavailableError, ThreadOwnership
from boba.identity.context import (
    CallContext,
    Scope,
    Subject,
)
from boba.identity.run import RunRegistry
from boba.studio.api.auth import ApiIdentity, CurrentUser
from boba.studio.api.urls import ToolCallUrl
from boba.toolkit.calls import ToolIntent
from boba.toolrun.invoke import (
    CallIdPrefix,
    InvokeReply,
    ToolInvoker,
    ToolUnavailableError,
)
from boba.toolrun.registry import ToolRegistry
from boba.workflow import ToolFacts
from boba.workflow_engine.catalog import CatalogBuilder

__all__ = ["ThreadsSource", "ToolCallBody", "ToolCallReply", "ToolCalling"]

logger = logging.getLogger(__name__)

RegistrySource = Callable[[], Awaitable[ToolRegistry]]
"""Реестр инструментов приложения; собирается контейнером на первый запрос."""

ThreadsSource = Callable[[], ThreadOwnership]
"""Владение тредами: реализует слой данных хоста, зовётся на вызов."""


class ToolCallBody(BaseModel):
    """Тело запроса: где и от какого профиля запускать, с чем."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thread_id: UUID
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
    def of(cls, reply: InvokeReply, call_id: str) -> ToolCallReply:
        return cls(
            call_id=call_id,
            ok=reply.ok,
            content=reply.content,
            result=reply.result.model_dump(mode="json"),
        )


class ToolCalling:
    """Обработчик POST /tools/{name}."""

    TAG: ClassVar[str] = "tools"

    def __init__(
        self,
        registry: RegistrySource,
        profiles: ChatProfiles,
        threads: ThreadsSource,
    ) -> None:
        self._registry = registry
        self._profiles = profiles
        self._threads = threads

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

    async def catalog(
        self, current_user: CurrentUser, profile: str | None = None
    ) -> Mapping[str, ToolFacts]:
        identity = ApiIdentity.resolve(current_user, profile, self._profiles)
        registry = await self._registry()

        return CatalogBuilder.of(
            registry, identity.subject.roles, identity.subject.profile
        )

    async def serve(
        self,
        name: str,
        body: ToolCallBody,
        current_user: CurrentUser,
    ) -> ToolCallReply:
        identity = ApiIdentity.resolve(current_user, body.profile, self._profiles)

        thread_id = str(body.thread_id)
        await self._own_thread(identity.subject.login, thread_id)

        invoker = await self._invoker(identity.subject)
        context = identity.context(Scope.chat(thread_id))

        return await self._run(invoker, name, body, context)

    async def _own_thread(self, login: str, thread_id: str) -> None:
        try:
            author = await self._threads().get_thread_author(thread_id)
        except DataRejectedError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
        except DataUnavailableError as exc:
            raise HTTPException(status_code=503, detail="Data layer is down") from exc

        if author != login:
            raise HTTPException(status_code=404, detail="Thread not found")

    async def _invoker(self, subject: Subject) -> ToolInvoker:
        registry = await self._registry()

        return ToolInvoker(registry.for_headless(subject.roles, subject.profile))

    @staticmethod
    async def _run(
        invoker: ToolInvoker, name: str, body: ToolCallBody, context: CallContext
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

        with RunRegistry.open(context):
            reply = await invoker.invoke(call)

        return ToolCallReply.of(reply, call_id)
