"""Контекст вызова: от чьего имени, в какой области и кем запущена работа.

Ставит тот, кто запускает — ход чата, страница, планировщик; цепочка
инструментов читает только его и про источник запуска не знает. Субъект
отвечает за права и секреты, область — за артефакты (журнал, workspace),
инициатор — за след «кто нажал».

Ошибки:
RefusalError — вызов идёт вне контекста, kind ContextKind.NO_CONTEXT.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.cancellation import RunCancellation
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.session import LogUserMark

__all__ = [
    "CallContext",
    "ChatInitiator",
    "ContextKind",
    "Credential",
    "DelegatedTicket",
    "HumanInitiator",
    "Initiator",
    "LlmInitiator",
    "NoUserCredential",
    "ScheduleInitiator",
    "Scope",
    "ScopeKind",
    "Subject",
]


class ContextKind(StrEnum):
    """Отказы контекста вызова."""

    NO_CONTEXT = "no_context"


class ScopeKind(StrEnum):
    """Что за область: тред чата, запуск workflow, запуск задания."""

    CHAT = "chat"
    WORKFLOW = "workflow"
    JOB = "job"


class Scope(BaseModel):
    """Где живут артефакты вызова: журнал, workspace, реестр запусков."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ScopeKind
    id: str = Field(min_length=1)

    SEPARATOR: ClassVar[str] = "/"

    @field_validator("id")
    @classmethod
    def _segment_is_safe(cls, value: str) -> str:
        """id становится сегментом путей журнала и workspace: наружу вести не может."""
        if value in (".", ".."):
            raise ValueError(f"invalid scope id: {value!r}")

        if cls.SEPARATOR in value:
            raise ValueError(f"invalid scope id: {value!r}")

        return value

    @classmethod
    def chat(cls, thread_id: str) -> Scope:
        return cls(kind=ScopeKind.CHAT, id=thread_id)


class Subject(BaseModel):
    """От чьего имени идёт вызов: права и секреты считаются по нему."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: int
    """Строка users."""
    login: str = Field(min_length=1)
    roles: frozenset[str]
    profile: str = Field(min_length=1)
    """Именованный набор грантов инструментов."""

    @property
    def user_key(self) -> str:
        """user_id в виде сегмента ключей журнала, storage и workspace."""
        return str(self.user_id)


class ChatInitiator(BaseModel):
    """Сообщение пользователя в чате."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["chat"] = "chat"
    thread_id: str
    turn_id: str


class LlmInitiator(BaseModel):
    """Вызов инструмента моделью внутри хода."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["llm"] = "llm"
    thread_id: str
    tool_call_id: str


class HumanInitiator(BaseModel):
    """Человек со страницы или через API."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["human"] = "human"
    via: Literal["page", "api"]


class ScheduleInitiator(BaseModel):
    """Планировщик по заданию."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["schedule"] = "schedule"
    job_id: str
    job_run_id: str


Initiator = Annotated[
    ChatInitiator | LlmInitiator | HumanInitiator | ScheduleInitiator,
    Field(discriminator="kind"),
]


class DelegatedTicket(BaseModel):
    """Делегированный kerberos-билет SSO-входа: ссылка на него, не сам билет."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["delegated"] = "delegated"
    principal: str
    sso_login: str


class NoUserCredential(BaseModel):
    """Секретов пользователя у вызова нет; reason — почему, для отказа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["none"] = "none"
    reason: str


Credential = Annotated[DelegatedTicket | NoUserCredential, Field(discriminator="kind")]


class CallContext(BaseModel):
    """Контекст вызова в contextvar: субъект, область, инициатор, секреты, отмена."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    subject: Subject
    scope: Scope
    initiator: Initiator
    credential: Credential
    cancellation: RunCancellation

    _CURRENT: ClassVar[ContextVar[CallContext | None]] = ContextVar(
        "boba_call_context", default=None
    )

    @classmethod
    def current(cls) -> CallContext:
        """Контекст текущего вызова; вне контекста — RefusalError."""
        context = cls._CURRENT.get()
        if context is None:
            raise RefusalError(
                ContextKind.NO_CONTEXT, "the call runs outside a call context"
            )

        return context

    @classmethod
    def peek(cls) -> CallContext | None:
        """Контекст, если он есть: для журнала и логов, которым без него можно."""
        return cls._CURRENT.get()

    @classmethod
    def current_subject(cls) -> Subject:
        return cls.current().subject

    @classmethod
    def reset(cls) -> None:
        """Снять контекст: пользуются тесты."""
        cls._CURRENT.set(None)

    def log_mark(self) -> LogUserMark:
        return LogUserMark(self.subject.login, self.scope.id)

    @contextmanager
    def applied(self) -> Iterator[CallContext]:
        """Ставит контекст и метку лога на время блока."""
        token = self._CURRENT.set(self)
        try:
            with self.log_mark().applied():
                yield self
        finally:
            self._CURRENT.reset(token)
