"""События обращений к модели: колбэки langchain переводятся в события чата.

Граф хода сообщает о прогонах модели колбэками langchain. TurnEvents —
единственный колбэк-обработчик хода: каждое событие он переводит в тип
диалога чата (boba.chainlit.chat.dialog) и отдаёт слушателям хода — трасеру
ленты и журналу состояний. Он же несёт порт этапа подготовки хода: подготовка
на графе находит его среди колбэков текущего прогона.

Ошибки: своих не выпускает; сбой слушателя уходит колбэк-менеджеру langchain.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import ChatGenerationChunk, GenerationChunk, LLMResult
from pydantic import BaseModel, ConfigDict
from typing_extensions import override

from boba.chainlit.agent.bridge import LangchainMessages
from boba.chainlit.chat.dialog import (
    ModelCallFinished,
    ModelCallStarted,
    ModelListener,
    ModelToken,
    PrefetchStage,
)

__all__ = ["TurnEvents"]


class InvocationParams(BaseModel):
    """Параметры вызова, которые langchain кладёт в колбэк старта прогона."""

    model_config = ConfigDict(extra="ignore")

    model: str = ""
    tools: Sequence[Mapping[str, Any]] = ()


class TurnEvents(AsyncCallbackHandler):
    """Колбэк-обработчик langchain одного хода: переводит события прогонов
    модели в события диалога чата и раздаёт их слушателям по порядку.

    Создаёт его агент сессии (GraphAgent) на каждый ход из слушателей и
    порта этапа подготовки, которые назвал чат, и отдаёт в callbacks прогона
    графа. Подготовка хода (PrefetchMiddleware) берёт у него stage. Вызовы
    инструментов сюда не приходят: их исполняет порт инструментов.
    """

    PARAMS_KEY: ClassVar[str] = "invocation_params"
    """Ключ колбэка старта прогона, под которым langchain кладёт параметры."""

    def __init__(
        self, listeners: Sequence[ModelListener], stage: PrefetchStage
    ) -> None:
        super().__init__()
        self._listeners = tuple(listeners)
        self._stage = stage
        self._messages = LangchainMessages()

    @property
    def stage(self) -> PrefetchStage:
        """Порт этапа подготовки хода, которому принадлежит прогон."""
        return self._stage

    @override
    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        sent = 0
        for batch in messages:
            sent += len(batch)

        params = self._params(kwargs)
        call = ModelCallStarted(
            run=str(run_id),
            model=params.model,
            messages=sent,
            tools=len(params.tools),
        )

        for listener in self._listeners:
            await listener.model_called(call)

    @override
    async def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        message = self._messages.of_chunk(chunk)

        message_id: str | None = None
        if message is not None:
            message_id = message.id

        event = ModelToken(
            run=str(run_id),
            message_id=message_id,
            text=self._text_of(token),
            reasoning=self._messages.reasoning_of(message),
        )

        for listener in self._listeners:
            await listener.model_token(event)

    @override
    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        finish = ModelCallFinished(
            run=str(run_id), reply=self._messages.of_result(response)
        )

        for listener in self._listeners:
            await listener.model_replied(finish)

    @override
    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        for listener in self._listeners:
            await listener.model_failed(str(run_id), error)

    def _params(self, kwargs: Mapping[str, Any]) -> InvocationParams:
        raw = kwargs.get(self.PARAMS_KEY)
        if not isinstance(raw, Mapping):
            return InvocationParams()

        return InvocationParams.model_validate(raw)

    @staticmethod
    def _text_of(token: str | list[str | dict[str, Any]]) -> str:
        if isinstance(token, str):
            return token

        parts: list[str] = []
        for part in token:
            if isinstance(part, str):
                parts.append(part)

        return "".join(parts)
