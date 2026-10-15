"""Производитель сообщений хода: единственное место, откуда ход публикует в шину;
большие тела уходят в PayloadStore, сообщение несёт ссылку. Ход агента ACP
публикует вопрос, уведомления `session/update` как есть (AgentUpdate), правку
вопроса, пометки остановки и итог.

Ошибки:
MessageBusError — шина не приняла сообщение; вызывающий показывает сбой сам.
"""

from __future__ import annotations

from typing import ClassVar

from acp.experimental.v2 import schema
from pydantic import BaseModel, ConfigDict

from boba.chainlit.agent.client import SessionUpdate
from boba.chainlit.rendering.acp_view import SessionUpdateCodec
from boba.identity.context import Scope
from boba.messaging import (
    AgentUpdate,
    AnswerInterrupted,
    AnyMessage,
    LockToken,
    MessageBus,
    Notice,
    NoticeLevel,
    PayloadStore,
    StreamAppended,
    StreamFeed,
    ThreadRewound,
    TurnFinished,
    TurnOutcome,
    TurnStarted,
)

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


class QuestionBody(BaseModel):
    """Тело вопроса хода в шине: текст, вложения блоками resource_link — те же,
    что приходят в воспроизведении, — и автор — имя шага вопроса во всех
    вкладках.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    links: tuple[schema.ResourceContentBlock, ...] = ()
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
        if isinstance(update, schema.SessionToolCallUpdate):
            ref = await self._payloads.put(self._scope, body)
            await self._publish(AgentUpdate(turn_id=self._turn_id, stored=ref))
            return

        await self._publish(AgentUpdate(turn_id=self._turn_id, inline=body))

    async def started(self, key: str, question: QuestionBody) -> None:
        ref = await self._payloads.put(
            self._scope, question.model_dump(mode="json", by_alias=True)
        )
        await self._publish(TurnStarted(turn_id=self._turn_id, key=key, question=ref))

    async def rewound(self, session_id: str) -> None:
        """История треда заменена веткой session_id до правленого вопроса."""
        await self._publish(ThreadRewound(turn_id=self._turn_id, session_id=session_id))

    async def answer_interrupted(self, key: str, note: str) -> None:
        await self._publish(
            AnswerInterrupted(turn_id=self._turn_id, key=key, note=note)
        )

    async def stream_appended(self, message: StreamAppended) -> None:
        await self._publish(message)

    async def notice(self, level: NoticeLevel, text: str) -> None:
        await self._publish(Notice(level=level, text=TextClip.fit(text)))

    async def finished(self, outcome: TurnOutcome, reason: str) -> None:
        message = TurnFinished(
            turn_id=self._turn_id, outcome=outcome, reason=TextClip.fit(reason)
        )
        await self._publish(message)
