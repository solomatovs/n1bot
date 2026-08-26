"""REST-запуск одного инструмента человеком: тот же реестр и цепочка, что у чата.

POST /tools/{name}: тред, профиль, intent и аргументы в теле, пользователь —
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
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit.infra.api_auth import ChainlitUsers
from boba.chat.profiles import ChatProfiles
from boba.chat.threads import DataRejectedError, DataUnavailableError, ThreadOwnership
from boba.identity.api import ApiSubject, AuthenticatedUser
from boba.identity.context import (
    CallContext,
    Scope,
    Subject,
)
from boba.identity.errors import AuthenticationError, RefusalError
from boba.identity.run import RunRegistry
from boba.toolkit.calls import ToolIntent
from boba.toolrun.invoke import (
    CallIdPrefix,
    InvokeReply,
    ToolInvoker,
    ToolUnavailableError,
)
from boba.toolrun.registry import ToolRegistry
from chainlit.auth import get_current_user
from chainlit.user import PersistedUser, User

__all__ = ["ApiIdentity", "CurrentUser", "ToolCallBody", "ToolCallReply", "ToolCalling"]

logger = logging.getLogger(__name__)

RegistrySource = Callable[[], Awaitable[ToolRegistry]]
"""Реестр инструментов приложения; собирается контейнером на первый запрос."""

LayerSource = Callable[[], ThreadOwnership]


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


class ApiIdentity:
    """Вход вызова API: 401 без сохранённого входа, 403 если профиль недоступен."""

    @staticmethod
    async def current(
        current_user: Annotated[User | PersistedUser | None, Depends(get_current_user)],
    ) -> AuthenticatedUser | None:
        """Зависимость FastAPI: пользователь входа из cookie chainlit."""
        return ChainlitUsers.of(current_user)

    @staticmethod
    def resolve(
        user: AuthenticatedUser | None, profile: str | None, profiles: ChatProfiles
    ) -> ApiSubject:
        if user is None:
            raise HTTPException(status_code=401, detail="Unauthorized")

        try:
            selected = profiles.resolve_or_default(profile, user.roles).name
        except RefusalError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        try:
            return ApiSubject.of(user, selected)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc


CurrentUser = Annotated[AuthenticatedUser | None, Depends(ApiIdentity.current)]


class ToolCalling:
    """Обработчик POST /tools/{name}."""

    def __init__(
        self,
        registry: RegistrySource,
        profiles: ChatProfiles,
        data_layer: LayerSource,
    ) -> None:
        self._registry = registry
        self._profiles = profiles
        self._data_layer = data_layer

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
            author = await self._data_layer().get_thread_author(thread_id)
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
