"""Производитель сообщений хода: единственное место, откуда ход публикует в шину;
большие тела уходят в PayloadStore, сообщение несёт ссылку. Ход агента ACP
публикует вопрос, уведомления `session/update` как есть (AgentUpdate), правку
вопроса, элементы итогов, пометки остановки и итог.

Ошибки:
MessageBusError — шина не приняла сообщение; вызывающий показывает сбой сам.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from acp.schema import ToolCallProgress, ToolCallStart
from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit.agent.client import SessionUpdate
from boba.chainlit.rendering.acp_view import SessionUpdateCodec
from boba.identity.context import Scope
from boba.messaging import (
    AgentUpdate,
    AnswerInterrupted,
    AnyMessage,
    ElementShown,
    LockToken,
    MessageBus,
    Notice,
    NoticeLevel,
    PayloadStore,
    StreamAppended,
    StreamFeed,
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
    """Тело вопроса хода в шине: текст, вложения, которые пользователь приложил к
    сообщению, и автор — имя шага вопроса во всех вкладках.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    elements: tuple[ShownElement, ...] = ()
    author: str = ""


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
        self._codec = SessionUpdateCodec()

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def turn_id(self) -> str:
        return self._turn_id

    async def _publish(self, message: AnyMessage) -> None:
        await self._bus.publish(self._scope, message, self._token)

    async def agent_update(self, update: SessionUpdate) -> None:
        """Уведомление агента как есть: вызовы с аргументами и итогом — по
        ссылке (ключи аргументов обязаны сохранить порядок, jsonb шины его
        не хранит), куски текста — в самом сообщении."""
        body = self._codec.encode(update)
        if isinstance(update, ToolCallStart | ToolCallProgress):
            ref = await self._payloads.put(self._scope, body)
            await self._publish(AgentUpdate(turn_id=self._turn_id, stored=ref))
            return

        await self._publish(AgentUpdate(turn_id=self._turn_id, inline=body))

    async def started(self, key: str, question: QuestionBody) -> None:
        ref = await self._payloads.put(
            self._scope, question.model_dump(mode="json", by_alias=True)
        )
        await self._publish(TurnStarted(turn_id=self._turn_id, key=key, question=ref))

    async def answer_interrupted(self, key: str, note: str) -> None:
        await self._publish(
            AnswerInterrupted(turn_id=self._turn_id, key=key, note=note)
        )

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
