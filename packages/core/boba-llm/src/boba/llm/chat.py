"""Порт чата с моделью: конверт запроса, события ответа, ошибка слоя.

Чат-модель — транспорт «сообщения и инструменты -> поток ответа». История,
исполнение инструментов и ход — забота вызывающего слоя. Ответ по схеме —
режим того же запроса: reply_schema объявляет форму ответа, а как её навязать
модели, знает реализация (функция с tool_choice, format, грамматика).

Ошибки:
LlmError — модель не загрузилась, провайдер недоступен, ответил не по
    контракту или оборвал генерацию (фильтр, поток без финала); поток событий
    обрывается этой ошибкой.
LlmOutputLimitError — подкласс LlmError: ответ срезан пределом длины (finish_reason
    length, done_reason length, расход потолка локального прогона).
LlmContextOverflowError — подкласс LlmError: запрос не поместился в окно модели
    (ответ 4xx или ошибка сервера с текстом о длине контекста).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ChatDelta",
    "ChatEvent",
    "ChatImage",
    "ChatModel",
    "ChatReply",
    "ChatRequest",
    "ChatRole",
    "ChatTurn",
    "ChatUsage",
    "ContextOverflowSigns",
    "LlmContextOverflowError",
    "LlmError",
    "LlmOutputLimitError",
    "ToolCall",
    "ToolSpec",
]


class LlmError(Exception):
    """Обращение к модели не состоялось: загрузка, сеть, статус, мусорный ответ."""


class LlmOutputLimitError(LlmError):
    """Ответ оборван пределом длины: модель замолчала на полуслове."""


class LlmContextOverflowError(LlmError):
    """Запрос не поместился в окно модели."""


class ContextOverflowSigns:
    """Распознаёт по тексту ошибки сервера, что запрос не поместился в окно.

    Единого кода у совместимых серверов нет: OpenAI отвечает
    `context_length_exceeded`, vLLM и llama.cpp — своим текстом про длину
    контекста. Реализации портов зовут этот класс для 4xx и для ошибок в теле.
    """

    PHRASES: ClassVar[tuple[str, ...]] = (
        "context_length_exceeded",
        "context length",
        "context window",
        "context size",
        "maximum context",
        "prompt is too long",
        "input is too long",
        "too many tokens",
        "exceeds the limit",
    )

    def matches(self, text: str) -> bool:
        return any(self._hits(text.lower()))

    def _hits(self, lowered: str) -> Iterator[bool]:
        for phrase in self.PHRASES:
            yield phrase in lowered


class ChatRole(StrEnum):
    """Роли сообщений диалога."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolSpec(BaseModel):
    """Объявление инструмента для модели: имя, описание, json schema аргументов.

    Та же модель описывает форму ответа по схеме (ChatRequest.reply_schema):
    удалённому провайдеру она уходит функцией, локальному — грамматикой.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: Mapping[str, Any]


class ToolCall(BaseModel):
    """Вызов инструмента в ответе модели: разобранные аргументы, не сырой json."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Идентификатор вызова; ответ едет с ним же.")
    name: str = Field(description="Имя инструмента.")
    arguments: Mapping[str, Any] = Field(description="Аргументы вызова.")


class ChatImage(BaseModel):
    """Картинка в сообщении пользователя: байты и их media type."""

    model_config = ConfigDict(frozen=True)

    media_type: str
    data: bytes


class ChatTurn(BaseModel):
    """Одно сообщение истории в конверте запроса."""

    model_config = ConfigDict(frozen=True)

    role: ChatRole
    content: str = ""
    images: Sequence[ChatImage] = Field(
        default=(),
        description="Картинки сообщения пользователя; бэкенд без зрения отказывает.",
    )
    reasoning: str | None = Field(
        default=None,
        description=(
            "Рассуждения ассистента, которые провайдер возвращает модели. "
            "None — поля у сообщения не было, бэкенду оно не отправляется; "
            "пустая строка отправляется как есть: провайдер в режиме "
            "размышления требует поле у каждого сообщения ассистента."
        ),
    )
    tool_calls: Sequence[ToolCall] = Field(
        default=(),
        description="Вызовы инструментов сообщения ассистента.",
    )
    tool_call_id: str = Field(
        default="",
        description="Для роли tool: вызов, на который отвечает сообщение.",
    )


class ChatRequest(BaseModel):
    """Конверт запроса: сообщения, инструменты, форма ответа и сэмплинг."""

    model_config = ConfigDict(frozen=True)

    messages: Sequence[ChatTurn]
    tools: Sequence[ToolSpec] = ()
    reply_schema: ToolSpec | None = Field(
        default=None,
        description=(
            "Форма ответа: модель обязана ответить объектом по этой схеме. "
            "None — свободный ответ."
        ),
    )
    sampling: Mapping[str, Any] = Field(
        default_factory=dict,
        description=(
            "Параметры запроса к провайдеру как есть: ключи и значения уходят "
            "в тело без проверок; что принимает провайдер — решает конфиг."
        ),
    )
    stream: bool = Field(
        default=True,
        description=(
            "Просить у бэкенда дельты; False — один запрос-ответ без потока, "
            "бэкенд отдаёт только финальный ChatReply."
        ),
    )


class ChatDelta(BaseModel):
    """Потоковый кусок ответа: текст и рассуждения растут дельтами.

    Вызовы инструментов дельтами не отдаются: их аргументы копит реализация
    и отдаёт целиком в финальном ChatReply.
    """

    model_config = ConfigDict(frozen=True)

    content: str = ""
    reasoning: str = ""


class ChatUsage(BaseModel):
    """Учёт токенов обращения; нули — провайдер учёт не прислал."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = Field(
        default=0,
        description=(
            "Часть output_tokens, ушедшая в рассуждения; 0 — провайдер "
            "рассуждения отдельно не считает."
        ),
    )


class ChatReply(BaseModel):
    """Финальное сообщение ответа целиком; всегда последнее событие потока."""

    model_config = ConfigDict(frozen=True)

    content: str = ""
    reasoning: str = ""
    tool_calls: Sequence[ToolCall] = ()
    usage: ChatUsage = ChatUsage()


ChatEvent: TypeAlias = ChatDelta | ChatReply


class ChatModel(ABC):
    """Порт чата: конверт запроса -> поток событий.

    Поток заканчивается ровно одним ChatReply; нестримящий бэкенд отдаёт
    только его, без дельт.
    """

    @abstractmethod
    def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]: ...

    async def reply(self, request: ChatRequest) -> ChatReply:
        """Финал ответа одним значением; поток без ChatReply — нарушение порта."""
        final: ChatReply | None = None
        async for event in self.chat(request):
            if isinstance(event, ChatReply):
                final = event

        if final is None:
            msg = "chat model ended the event stream without a ChatReply"
            raise LlmError(msg)

        return final
