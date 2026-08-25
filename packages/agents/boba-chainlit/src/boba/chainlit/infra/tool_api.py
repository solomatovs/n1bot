"""REST-запуск одного инструмента человеком: тот же реестр и цепочка, что у чата.

POST /tools/{name}: тред, профиль, intent и аргументы в теле, пользователь —
из cookie входа. Контекст вызова собирается здесь под HumanInitiator(api);
инструменты чата (chat_only) недоступны — эмитить им некуда.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — профиль недоступен ролям пользователя.
404 — тред не пользователя, инструмент не собран или не разрешён.
400 — инструмент работает только в чате.
503 — слой данных недоступен.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException
from langchain_core.messages import ToolCall, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import RunCancellation
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.data.errors import DataRejectedError, DataUnavailableError
from boba.chainlit.domain.context import (
    CallContext,
    Credential,
    DelegatedTicket,
    HumanInitiator,
    NoUserCredential,
    Scope,
    Subject,
)
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.run import RunPort, RunRegistry
from boba.chainlit.domain.session import SsoMarks
from boba.chainlit.infra.config import ChatProfiles
from boba.chainlit.infra.plugins import ToolRegistry
from boba.chainlit.infra.session import ChainlitSession
from boba.toolkit.calls import ToolIntent
from boba.toolkit.result import ErrorResult, ToolArtifact
from chainlit.auth import get_current_user
from chainlit.user import PersistedUser, User

__all__ = ["HeadlessPort", "ToolCallBody", "ToolCallReply", "ToolCalling"]

logger = logging.getLogger(__name__)

RegistrySource = Callable[[], Awaitable[ToolRegistry]]
"""Реестр инструментов приложения; собирается контейнером на первый запрос."""

LayerSource = Callable[[], PostgresDataLayer]


class ApiErrorKind(StrEnum):
    """Коды отказов, которые API добавляет к результатам инструментов."""

    NO_RESULT = "no_result"


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
    def of(cls, message: ToolMessage, call_id: str) -> ToolCallReply:
        result = ToolArtifact.revive(message.artifact)
        if result is None:
            result = ErrorResult(
                message="the tool returned no result",
                error_kind=ApiErrorKind.NO_RESULT,
            )

        return cls(
            call_id=call_id,
            ok=result.ok,
            content=str(message.content),
            result=result.model_dump(mode="json"),
        )


class HeadlessPort(RunPort):
    """Владелец запуска без ленты чата: крепить элементы некуда."""

    answer_step_id = None


class ApiCall:
    """Идентификаторы вызовов через API: отличимы от вызовов модели."""

    PREFIX: ClassVar[str] = "api-"

    @classmethod
    def new_id(cls) -> str:
        return f"{cls.PREFIX}{uuid4().hex}"


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
        user = self._persisted(current_user)
        roles = ChainlitSession.roles_of(user)
        profile = self._profile(body.profile, roles)

        thread_id = str(body.thread_id)
        await self._own_thread(user, thread_id)

        tool = await self._tool(name, roles, profile)
        context = self._context(user, roles, profile, thread_id)

        return await self._run(tool, body, context)

    @staticmethod
    def _persisted(current_user: User | PersistedUser | None) -> PersistedUser:
        if not isinstance(current_user, PersistedUser):
            raise HTTPException(status_code=401, detail="Unauthorized")

        return current_user

    def _profile(self, name: str, roles: frozenset[str]) -> str:
        try:
            return self._profiles.resolve(name, roles).name
        except RefusalError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    async def _own_thread(self, user: PersistedUser, thread_id: str) -> None:
        try:
            author = await self._data_layer().get_thread_author(thread_id)
        except DataRejectedError as exc:
            raise HTTPException(status_code=404, detail="Thread not found") from exc
        except DataUnavailableError as exc:
            raise HTTPException(status_code=503, detail="Data layer is down") from exc

        if author != user.identifier:
            raise HTTPException(status_code=404, detail="Thread not found")

    async def _tool(self, name: str, roles: frozenset[str], profile: str) -> BaseTool:
        registry = await self._registry()

        if name in registry.chat_only:
            raise HTTPException(
                status_code=400, detail=f"tool {name!r} works only inside a chat"
            )

        for tool in registry.for_session(roles, profile):
            if tool.name == name:
                return tool

        raise HTTPException(status_code=404, detail=f"tool {name!r} is not available")

    @classmethod
    def _context(
        cls,
        user: PersistedUser,
        roles: frozenset[str],
        profile: str,
        thread_id: str,
    ) -> CallContext:
        subject = Subject(
            user_id=int(user.id),
            login=user.identifier,
            roles=roles,
            profile=profile,
        )

        return CallContext(
            subject=subject,
            scope=Scope.chat(thread_id),
            initiator=HumanInitiator(via="api"),
            credential=cls._credential(user),
            cancellation=RunCancellation(),
        )

    @staticmethod
    def _credential(user: PersistedUser) -> Credential:
        """Ссылка на билет входа по меткам пользователя; иначе — причина отказа."""
        metadata = ChainlitSession.metadata_of(user)

        marks = SsoMarks.of_metadata(metadata)
        if marks is not None:
            return DelegatedTicket(principal=marks.principal, sso_login=marks.login)

        return NoUserCredential(reason=SsoMarks.absence_reason(metadata))

    @staticmethod
    async def _run(
        tool: BaseTool, body: ToolCallBody, context: CallContext
    ) -> ToolCallReply:
        call_id = ApiCall.new_id()
        args: dict[str, Any] = dict(body.args)
        args[ToolIntent.NAME] = body.intent
        call = ToolCall(name=tool.name, args=args, id=call_id, type="tool_call")

        logger.info(
            "api tool call: %s by %s in thread %s",
            tool.name,
            context.subject.login,
            context.scope.id,
        )

        with (
            context.applied(),
            RunRegistry.open(context.scope.id, HeadlessPort(), context.cancellation),
        ):
            message = await tool.ainvoke(call)

        if not isinstance(message, ToolMessage):
            msg = f"tool {tool.name!r} returned {type(message).__name__}"
            raise HTTPException(status_code=500, detail=msg)

        return ToolCallReply.of(message, call_id)
