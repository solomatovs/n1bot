"""Один запрос к модели: поток ответа, частичный текст, разбор ошибок порта, отмена.

Цикл хода и пересказ при сжатии зовут модель одинаково и различаются только
тем, что делают с исходом: ход повторяет запрос при ошибке и показывает куски
ответа наружу, пересказ считает любую ошибку неудачей и кусков не показывает.
Общая часть живёт здесь: ModelAsk делает ровно одну попытку и возвращает её
исход закрытым перечнем вариантов (план, разделы 5.13, 5.14, 5.16).

Ошибки наружу не выходят: каждая ошибка порта превращается в вариант исхода;
чужая отмена задачи (не остановка хода) идёт дальше как CancelledError.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from boba.agent.events import EventSink, StreamEvent
from boba.cancellation import RunCancellation, StopReason, ToolStopped
from boba.llm.chat import (
    ChatDelta,
    ChatModel,
    ChatReply,
    ChatRequest,
    LlmContextOverflowError,
    LlmError,
    LlmOutputLimitError,
)

__all__ = [
    "Aborted",
    "Answered",
    "Asked",
    "Errored",
    "ModelAsk",
    "OutputLimited",
    "Overflowed",
    "Partial",
]


@dataclass
class Partial:
    """Текст и рассуждения, пришедшие кусками до обрыва или конца потока."""

    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)

    def take(self, delta: ChatDelta) -> None:
        if delta.content:
            self.content.append(delta.content)

        if delta.reasoning:
            self.reasoning.append(delta.reasoning)

    def is_empty(self) -> bool:
        return not self.content and not self.reasoning

    def as_reply(self) -> ChatReply:
        return ChatReply(
            content="".join(self.content), reasoning="".join(self.reasoning)
        )


@dataclass(frozen=True)
class Answered:
    """Модель ответила целиком."""

    reply: ChatReply


@dataclass(frozen=True)
class OutputLimited:
    """Ответ оборван пределом длины; пришедшее — в partial."""

    partial: Partial
    error: str


@dataclass(frozen=True)
class Overflowed:
    """Запрос не поместился в окно модели."""

    error: str


@dataclass(frozen=True)
class Errored:
    """Обращение не состоялось: сеть, статус, мусорный ответ, поток без финала."""

    error: str


@dataclass(frozen=True)
class Aborted:
    """Ход остановлен во время ответа."""

    partial: Partial
    reason: StopReason


Asked = Answered | OutputLimited | Overflowed | Errored | Aborted


class ModelAsk:
    """Одна попытка запроса к модели с остановкой по RunCancellation.

    Создаёт цикл хода; им же пользуется пересказ при сжатии. Куски ответа
    уходят наружу событиями, только если попытку просили показывать.
    """

    def __init__(self, model: ChatModel, sink: EventSink) -> None:
        self._model = model
        self._sink = sink

    async def attempt(
        self, request: ChatRequest, cancellation: RunCancellation, show: bool
    ) -> Asked:
        partial = Partial()
        try:
            reply = await self._stream(request, partial, cancellation, show)
        except (ToolStopped, asyncio.CancelledError) as exc:
            return self._aborted(exc, partial, cancellation)
        except LlmContextOverflowError as exc:
            return Overflowed(str(exc))
        except LlmOutputLimitError as exc:
            return OutputLimited(partial, str(exc))
        except LlmError as exc:
            return Errored(str(exc))

        return Answered(reply)

    async def _stream(
        self,
        request: ChatRequest,
        partial: Partial,
        cancellation: RunCancellation,
        show: bool,
    ) -> ChatReply:
        task = asyncio.current_task()
        if task is None:
            msg = "The model was asked outside an asyncio task. This is a bug."
            raise RuntimeError(msg)

        reply: ChatReply | None = None
        with cancellation.abort_with(self._canceller(task)):
            async for event in self._model.chat(request):
                if isinstance(event, ChatReply):
                    reply = event
                    continue

                partial.take(event)
                if show:
                    await self._sink.emit(
                        StreamEvent(content=event.content, reasoning=event.reasoning)
                    )

        if reply is None:
            msg = (
                "The model stream ended without a final reply: the provider closed "
                "the connection before the answer was complete."
            )
            raise LlmError(msg)

        return reply

    def _aborted(
        self, exc: BaseException, partial: Partial, cancellation: RunCancellation
    ) -> Aborted:
        """Остановка хода; чужая отмена задачи идёт дальше."""
        if isinstance(exc, asyncio.CancelledError):
            if not cancellation.cancelled:
                raise exc

            self._uncancel()

        return Aborted(partial, self._reason(cancellation))

    @staticmethod
    def _canceller(task: asyncio.Task[object]) -> Callable[[], None]:
        def cancel() -> None:
            task.cancel()

        return cancel

    @staticmethod
    def _uncancel() -> None:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()

    @staticmethod
    def _reason(cancellation: RunCancellation) -> StopReason:
        reason = cancellation.reason
        if reason is None:
            return StopReason.USER_STOP

        return reason
