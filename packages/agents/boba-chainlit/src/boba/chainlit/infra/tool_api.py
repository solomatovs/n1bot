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

from boba.cancellation import RunCancellation
from boba.chainlit.agent.invoke import (
    CallIdPrefix,
    InvokeReply,
    ToolInvoker,
    ToolUnavailableError,
)
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.data.errors import DataRejectedError, DataUnavailableError
from boba.chainlit.domain.context import (
    CallContext,
    Credential,
    DelegatedTicket,
    HumanInitiator,
    Scope,
    Subject,
)
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.run import RunRegistry
from boba.chainlit.infra.config import ChatProfiles
from boba.chainlit.infra.plugins import ToolRegistry
from boba.chainlit.infra.session import ChainlitSession
from boba.toolkit.calls import ToolIntent
from chainlit.auth import get_current_user
from chainlit.user import PersistedUser, User

__all__ = ["ApiIdentity", "ToolCallBody", "ToolCallReply", "ToolCalling"]

logger = logging.getLogger(__name__)

RegistrySource = Callable[[], Awaitable[ToolRegistry]]
"""Реестр инструментов приложения; собирается контейнером на первый запрос."""

LayerSource = Callable[[], PostgresDataLayer]


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
    """Кто зовёт API: сохранённый пользователь, субъект под профилем, секреты.

    Ошибки — HTTPException: 401 без сохранённого входа, 403 профиль недоступен.
    """

    def __init__(
        self, user: PersistedUser, subject: Subject, credential: Credential
    ) -> None:
        self.user = user
        self.subject = subject
        self.credential = credential

    @classmethod
    def resolve(
        cls,
        current_user: User | PersistedUser | None,
        profile: str | None,
        profiles: ChatProfiles,
    ) -> ApiIdentity:
        user = cls._persisted(current_user)
        roles = ChainlitSession.roles_of(user)
        selected = cls._profile(profiles, profile, roles)

        try:
            subject = Subject.of_user(user.id, user.identifier, roles, selected)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        credential = DelegatedTicket.credential_of(ChainlitSession.metadata_of(user))
        return cls(user, subject, credential)

    def context(self, scope: Scope) -> CallContext:
        """Контекст вызова человека через API в заданной области."""
        return CallContext(
            subject=self.subject,
            scope=scope,
            initiator=HumanInitiator(via="api"),
            credential=self.credential,
            cancellation=RunCancellation(),
        )

    @staticmethod
    def _persisted(current_user: User | PersistedUser | None) -> PersistedUser:
        if not isinstance(current_user, PersistedUser):
            raise HTTPException(status_code=401, detail="Unauthorized")

        return current_user

    @staticmethod
    def _profile(
        profiles: ChatProfiles, name: str | None, roles: frozenset[str]
    ) -> str:
        """Названный профиль обязан быть виден ролям; без имени — по умолчанию."""
        try:
            return profiles.resolve_or_default(name, roles).name
        except RefusalError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc


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
        current_user: Annotated[User | PersistedUser | None, Depends(get_current_user)],
    ) -> ToolCallReply:
        identity = ApiIdentity.resolve(current_user, body.profile, self._profiles)

        thread_id = str(body.thread_id)
        await self._own_thread(identity.user, thread_id)

        invoker = await self._invoker(identity.subject)
        context = identity.context(Scope.chat(thread_id))

        return await self._run(invoker, name, body, context)

    async def _own_thread(self, user: PersistedUser, thread_id: str) -> None:
        try:
            author = await self._data_layer().get_thread_author(thread_id)
        except DataRejectedError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
        except DataUnavailableError as exc:
            raise HTTPException(status_code=503, detail="Data layer is down") from exc

        if author != user.identifier:
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
