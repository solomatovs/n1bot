"""Контекст вызова: от чьего имени, в какой области и кем запущена работа.

Ставит тот, кто запускает — ход чата, страница, планировщик; цепочка
инструментов читает только его и про источник запуска не знает. Субъект
отвечает за права и секреты, область — за артефакты (журнал, workspace),
инициатор — за след «кто нажал».

Ошибки:
RefusalError — вызов идёт вне контекста (ContextKind.NO_CONTEXT), инструменту
    чата достался контекст не чата (ContextKind.CHAT_ONLY) либо вызов пришёл
    не от модели и id вызова у него нет (ContextKind.NO_TOOL_CALL).
"""

from __future__ import annotations

from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.cancellation import RunCancellation
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.session import LogUserMark, SignInProvider, UserMetadataField

__all__ = [
    "CallContext",
    "ChatCallContext",
    "ChatInitiator",
    "ChatSurface",
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
    CHAT_ONLY = "chat_only"
    NO_TOOL_CALL = "no_tool_call"


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

    @classmethod
    def workflow(cls, run_id: UUID) -> Scope:
        """Область запуска workflow: run_id — ключ реестра запусков и журнала."""
        return cls(kind=ScopeKind.WORKFLOW, id=str(run_id))


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

    @classmethod
    def of_user(
        cls, user_id: str, login: str, roles: Iterable[str], profile: str
    ) -> Subject:
        """Субъект по строке users; id не число — ValueError."""
        try:
            numeric = int(user_id)
        except ValueError as exc:
            raise ValueError(
                f"user id {user_id!r} is not the users.id integer"
            ) from exc

        return cls(
            user_id=numeric, login=login, roles=frozenset(roles), profile=profile
        )


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


class NoUserCredential(BaseModel):
    """Секретов пользователя у вызова нет; reason — почему, для отказа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["none"] = "none"
    reason: str


class DelegatedTicket(BaseModel):
    """Делегированный kerberos-билет SSO-входа: ссылка на него, не сам билет.

    principal — чей билет, login — какому входу он выдан (ключ реестра
    билетов). Единственная модель этой пары: из metadata пользователя, из JWT
    и из контекста вызова читается она же.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["delegated"] = "delegated"
    principal: str
    login: str

    @classmethod
    def of_metadata(cls, metadata: Mapping[str, object]) -> DelegatedTicket | None:
        """Ссылка на билет из metadata входа; None — делегирования не было."""
        if metadata.get(UserMetadataField.PROVIDER) != SignInProvider.KERBEROS:
            return None

        principal = metadata.get(UserMetadataField.PRINCIPAL)
        if not isinstance(principal, str) or not principal:
            return None

        login = metadata.get(UserMetadataField.LOGIN)
        if not isinstance(login, str) or not login:
            return None

        return cls(principal=principal, login=login)

    @classmethod
    def credential_of(cls, metadata: Mapping[str, object]) -> Credential:
        """Секреты вызова по metadata входа: билет либо причина его отсутствия."""
        ticket = cls.of_metadata(metadata)
        if ticket is not None:
            return ticket

        return NoUserCredential(reason=cls.absence_reason(metadata))

    @classmethod
    def absence_reason(cls, metadata: Mapping[str, object]) -> str:
        """Почему у входа нет делегированного билета; текст готов для отказа."""
        provider = metadata.get(UserMetadataField.PROVIDER)
        if provider != SignInProvider.KERBEROS:
            return (
                f"you signed in with {cls._provider_name(provider)}, and this "
                "connection acts in the database on your behalf: sign in with "
                "the Kerberos SSO button instead"
            )

        principal = metadata.get(UserMetadataField.PRINCIPAL)
        if not isinstance(principal, str):
            return cls._no_principal()

        if not principal:
            return cls._no_principal()

        return (
            f"the Kerberos sign-in of {principal} carried no delegated ticket: "
            "either Active Directory does not allow this service to act for "
            "you, or the browser sent no ticket; sign in again from a "
            "domain-joined browser"
        )

    @staticmethod
    def _no_principal() -> str:
        return (
            "your Kerberos sign-in predates delegated connections "
            "(the session token names no principal): sign out and sign in again"
        )

    @staticmethod
    def _provider_name(provider: object) -> str:
        if not isinstance(provider, str):
            return "no known provider"

        if not provider:
            return "no known provider"

        return provider


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

    @classmethod
    def push(cls, context: CallContext) -> Token[CallContext | None]:
        """Ставит производный контекст на время вызова; снимает pop."""
        return cls._CURRENT.set(context)

    @classmethod
    def pop(cls, token: Token[CallContext | None]) -> None:
        cls._CURRENT.reset(token)

    def log_mark(self) -> LogUserMark:
        return LogUserMark(self.subject.login, self.scope.id)

    def as_tool_call(self, tool_call_id: str) -> CallContext:
        """Контекст вызова инструмента моделью: инициатор — llm, остальное то же.

        Вне хода чата инициатор не меняется: инструмент запустил не модель.
        """
        if not isinstance(self.initiator, ChatInitiator):
            return self

        if not tool_call_id:
            return self

        llm = LlmInitiator(
            thread_id=self.initiator.thread_id, tool_call_id=tool_call_id
        )
        return self.model_copy(update={"initiator": llm})

    def in_scope(self, scope: Scope) -> CallContext:
        """Тот же субъект, инициатор и секреты в другой области со своей отменой.

        Явный конструктор, не model_copy: контекст чата не должен пронести
        поверхность в запуск вне чата.
        """
        return CallContext(
            subject=self.subject,
            scope=scope,
            initiator=self.initiator,
            credential=self.credential,
            cancellation=RunCancellation(),
        )

    @contextmanager
    def applied(self) -> Generator[CallContext, None, None]:
        """Ставит контекст и метку лога на время блока."""
        token = self._CURRENT.set(self)
        try:
            with self.log_mark().applied():
                yield self
        finally:
            self._CURRENT.reset(token)


@runtime_checkable
class ChatSurface(Protocol):
    """Куда ход чата шлёт события фронту: сокет сессии или рассылка треда."""

    async def emit(self, event: str, payload: Mapping[str, Any]) -> bool:
        """True — событие ушло живому слушателю; False — слушать некому."""
        ...


class ChatCallContext(CallContext):
    """Контекст хода чата: вдобавок к общему — поверхность для событий фронту.

    Инструменты, которым нужен чат (панель, карточки, вложения), требуют
    именно его; вне чата они отказывают, а не молчат.
    """

    surface: ChatSurface

    @classmethod
    def require(cls) -> ChatCallContext:
        """Контекст чата; вызов вне чата — RefusalError(CHAT_ONLY)."""
        context = cls.current()
        if not isinstance(context, ChatCallContext):
            raise RefusalError(
                ContextKind.CHAT_ONLY, "this tool works only inside a chat turn"
            )

        return context

    def tool_call_id(self) -> str:
        """id вызова модели: к нему привязываются элементы, созданные инструментом."""
        if not isinstance(self.initiator, LlmInitiator):
            raise RefusalError(ContextKind.NO_TOOL_CALL, "tool call without id")

        return self.initiator.tool_call_id
