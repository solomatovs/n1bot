"""Производитель сообщений хода: единственное место, откуда ход публикует в шину;
большие тела уходят в PayloadStore, сообщение несёт ссылку.

Ошибки:
MessageBusError — шина не приняла сообщение; вызывающий показывает сбой сам.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.identity.context import Scope
from boba.messaging import (
    AnswerClosed,
    AnswerInterrupted,
    AnswerToken,
    AnyMessage,
    ElementShown,
    LockToken,
    MessageBus,
    ModelAnswered,
    Notice,
    NoticeLevel,
    PayloadStore,
    StageEnded,
    StageQueries,
    StageStarted,
    StreamAppended,
    StreamFeed,
    ThinkingClosed,
    ThinkingComplete,
    ThinkingToken,
    ToolFailed,
    ToolFinished,
    ToolStarted,
    ToolStopped,
    TurnFinished,
    TurnOutcome,
    TurnStarted,
)
from chainlit.element import ElementDict, ElementDisplay, ElementSize, ElementType

__all__ = ["TextClip", "TurnFeed"]


class TextClip:
    """Обрезает текст до предела в байтах, чтобы сообщение с ним гарантированно влезло в
    шину.
    """

    LIMIT_BYTES: ClassVar[int] = 4000
    ELLIPSIS: ClassVar[str] = "…"

    @classmethod
    def fit(cls, text: str) -> str:
        encoded = text.encode("utf-8")
        if len(encoded) <= cls.LIMIT_BYTES:
            return text

        head = encoded[: cls.LIMIT_BYTES].decode("utf-8", errors="ignore")
        return f"{head}{cls.ELLIPSIS}"


class ShownElement(BaseModel):
    """Элемент ленты, переданный шиной: поля ElementDict chainlit, проверенные на
    границе перед отправкой вкладкам; имена полей — как в ElementDict.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    id: str = Field(min_length=1)
    thread_id: str | None = Field(default=None, alias="threadId")
    type: ElementType
    url: str | None = None
    chainlit_key: str | None = Field(default=None, alias="chainlitKey")
    name: str
    display: ElementDisplay
    object_key: str | None = Field(default=None, alias="objectKey")
    size: ElementSize | None = None
    props: dict[str, Any] | None = None
    page: int | None = None
    auto_play: bool | None = Field(default=None, alias="autoPlay")
    player_config: dict[str, Any] | None = Field(default=None, alias="playerConfig")
    language: str | None = None
    for_id: str | None = Field(default=None, alias="forId")
    mime: str | None = None

    def to_element(self) -> ElementDict:
        return ElementDict(
            id=self.id,
            threadId=self.thread_id,
            type=self.type,
            url=self.url,
            chainlitKey=self.chainlit_key,
            name=self.name,
            display=self.display,
            objectKey=self.object_key,
            size=self.size,
            props=self.props,
            page=self.page,
            autoPlay=self.auto_play,
            playerConfig=self.player_config,
            language=self.language,
            forId=self.for_id,
            mime=self.mime,
        )


class QuestionBody(BaseModel):
    """Тело вопроса хода в шине: текст и вложения, которые пользователь приложил к
    сообщению.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    elements: tuple[ShownElement, ...] = ()


class TurnFeed(StreamFeed):
    """Публикует сообщения одного хода в область его треда от имени держателя хода."""

    def __init__(
        self,
        bus: MessageBus,
        payloads: PayloadStore,
        scope: Scope,
        turn_id: str,
        token: LockToken,
    ) -> None:
        self._bus = bus
        self._payloads = payloads
        self._scope = scope
        self._turn_id = turn_id
        self._token = token

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def turn_id(self) -> str:
        return self._turn_id

    def adopt(self, token: LockToken) -> None:
        """Принимает token захваченной блокировки: дальше публикации идут с ним."""
        self._token = token

    async def _publish(self, message: AnyMessage) -> None:
        await self._bus.publish(self._scope, message, self._token)

    async def started(self, key: str, question: QuestionBody) -> None:
        ref = await self._payloads.put(
            self._scope, question.model_dump(mode="json", by_alias=True)
        )
        message = TurnStarted(turn_id=self._turn_id, key=key, question=ref)
        await self._publish(message)

    async def model_answered(self) -> None:
        await self._publish(ModelAnswered(turn_id=self._turn_id))

    async def answer_token(self, key: str, token: str) -> None:
        await self._publish(AnswerToken(turn_id=self._turn_id, key=key, token=token))

    async def answer_closed(self, key: str) -> None:
        await self._publish(AnswerClosed(turn_id=self._turn_id, key=key))

    async def answer_interrupted(self, key: str, note: str) -> None:
        message = AnswerInterrupted(turn_id=self._turn_id, key=key, note=note)
        await self._publish(message)

    async def thinking_token(self, key: str, token: str) -> None:
        message = ThinkingToken(turn_id=self._turn_id, key=key, token=token)
        await self._publish(message)

    async def thinking_complete(self, key: str, text: str) -> None:
        ref = await self._payloads.put(self._scope, text)
        await self._publish(ThinkingComplete(turn_id=self._turn_id, key=key, text=ref))

    async def thinking_closed(self) -> None:
        await self._publish(ThinkingClosed(turn_id=self._turn_id))

    async def stage_started(self, name: str, phase: str) -> None:
        message = StageStarted(turn_id=self._turn_id, name=name, phase=phase)
        await self._publish(message)

    async def stage_queries(self, name: str, queries: Sequence[str]) -> None:
        message = StageQueries(turn_id=self._turn_id, name=name, queries=tuple(queries))
        await self._publish(message)

    async def stage_ended(
        self, name: str, queries: Sequence[str], elapsed_ms: int
    ) -> None:
        message = StageEnded(
            turn_id=self._turn_id,
            name=name,
            queries=tuple(queries),
            elapsed_ms=elapsed_ms,
        )
        await self._publish(message)

    async def tool_started(
        self, call_id: str, name: str, args: Mapping[str, Any]
    ) -> None:
        ref = await self._payloads.put(self._scope, dict(args))
        message = ToolStarted(
            turn_id=self._turn_id, call_id=call_id, name=name, args=ref
        )
        await self._publish(message)

    async def tool_finished(self, call_id: str, result: object) -> None:
        ref = await self._payloads.put(self._scope, result)
        message = ToolFinished(turn_id=self._turn_id, call_id=call_id, result=ref)
        await self._publish(message)

    async def tool_failed(self, call_id: str, error: str) -> None:
        message = ToolFailed(
            turn_id=self._turn_id, call_id=call_id, error=TextClip.fit(error)
        )
        await self._publish(message)

    async def tool_stopped(self, call_id: str, note: str) -> None:
        message = ToolStopped(turn_id=self._turn_id, call_id=call_id, note=note)
        await self._publish(message)

    async def stream_appended(self, message: StreamAppended) -> None:
        await self._publish(message)

    async def element_shown(self, call_id: str, element: Mapping[str, Any]) -> None:
        ref = await self._payloads.put(self._scope, dict(element))
        message = ElementShown(turn_id=self._turn_id, call_id=call_id, element=ref)
        await self._publish(message)

    async def notice(self, level: NoticeLevel, text: str) -> None:
        await self._publish(Notice(level=level, text=TextClip.fit(text)))

    async def finished(self, outcome: TurnOutcome, reason: str) -> None:
        message = TurnFinished(
            turn_id=self._turn_id, outcome=outcome, reason=TextClip.fit(reason)
        )
        await self._publish(message)
