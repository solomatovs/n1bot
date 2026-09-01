"""Стандарт LLM-провайдеров: конверты чата, порт ChatProvider, union-конфиг.

Каждая LLM-способность проекта собирается по одному шаблону: порт (базовый
класс), union-конфиг с дискриминатором provider ("local" | "openai") и
фабрика, отдающая реализацию по конфигу. Использующий код объявляет порт,
реализацию подставляет DI. Чат описан здесь; генерация по схеме — generation
(StructuredGenerator), эмбеддинг — embedding (Embedder).

Провайдер чата — транспорт: принять конверт запроса (сообщения, инструменты,
сэмплинг), отдать поток событий — дельты и финальное сообщение. История,
исполнение инструментов и ход — забота вызывающего слоя.

Ошибки:
ChatProviderError — бэкенд не загрузился, недоступен или ответил не по
    контракту; поток событий обрывается этой ошибкой.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from boba.chat.openai import OpenAiConfig

__all__ = [
    "ChatBackendConfig",
    "ChatDelta",
    "ChatEvent",
    "ChatProvider",
    "ChatProviderError",
    "ChatReply",
    "ChatRequest",
    "ChatRole",
    "ChatTurn",
    "ChatUsage",
    "LocalChatConfig",
    "OpenAiChatConfig",
    "ToolCallRequest",
    "ToolSpec",
]


class ChatProviderError(Exception):
    """Чат-инференс не состоялся: загрузка, сеть, статус или мусорный ответ."""


class ChatRole(StrEnum):
    """Роли сообщений диалога."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCallRequest(BaseModel):
    """Вызов инструмента в ответе модели: разобранные аргументы, не сырой json."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Идентификатор вызова; ответ едет с ним же.")
    name: str = Field(description="Имя инструмента.")
    arguments: Mapping[str, Any] = Field(description="Аргументы вызова.")


class ChatTurn(BaseModel):
    """Одно сообщение истории в конверте запроса."""

    model_config = ConfigDict(frozen=True)

    role: ChatRole
    content: str = ""
    reasoning: str | None = Field(
        default=None,
        description=(
            "Рассуждения ассистента, которые провайдер возвращает модели. "
            "None — поля у сообщения не было, бэкенду оно не отправляется; "
            "пустая строка отправляется как есть: провайдер в режиме "
            "размышления требует поле у каждого сообщения ассистента."
        ),
    )
    tool_calls: Sequence[ToolCallRequest] = Field(
        default=(),
        description="Вызовы инструментов сообщения ассистента.",
    )
    tool_call_id: str = Field(
        default="",
        description="Для роли tool: вызов, на который отвечает сообщение.",
    )


class ToolSpec(BaseModel):
    """Объявление инструмента для модели: имя, описание, json schema аргументов."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: Mapping[str, Any]


class ChatRequest(BaseModel):
    """Конверт запроса: сообщения, инструменты и сэмплинг одного обращения."""

    model_config = ConfigDict(frozen=True)

    messages: Sequence[ChatTurn]
    tools: Sequence[ToolSpec] = ()
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

    Вызовы инструментов дельтами не отдаются: их аргументы копит провайдер
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


class ChatReply(BaseModel):
    """Финальное сообщение ответа целиком; всегда последнее событие потока."""

    model_config = ConfigDict(frozen=True)

    content: str = ""
    reasoning: str = ""
    tool_calls: Sequence[ToolCallRequest] = ()
    usage: ChatUsage = ChatUsage()


ChatEvent: TypeAlias = ChatDelta | ChatReply


class ChatProvider(ABC):
    """Порт чат-инференса: конверт запроса -> поток событий.

    Поток заканчивается ровно одним ChatReply; нестримящий бэкенд отдаёт
    только его, без дельт.
    """

    @abstractmethod
    def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]: ...


class LocalChatConfig(BaseModel):
    """Локальный чат-бэкенд: in-process инференс onnxruntime-genai."""

    model_config = ConfigDict(extra="ignore")

    provider: Literal["local"]

    model_dir: str = Field(
        description=(
            "Каталог модели onnxruntime-genai: genai_config.json, веса, "
            "токенайзер и chat_template. Модель кладётся заранее."
        ),
    )


class OpenAiChatConfig(BaseModel):
    """Удалённый чат-бэкенд: openai-совместимый endpoint /chat/completions."""

    model_config = ConfigDict(extra="ignore")

    provider: Literal["openai"]

    openai: OpenAiConfig = Field(
        description=(
            "Транспорт openai-провайдера; в конфиге подключается ссылкой "
            "${openai.<name>}."
        ),
    )


ChatBackendConfig = Annotated[
    LocalChatConfig | OpenAiChatConfig,
    Field(discriminator="provider"),
]
"""Discriminated union по provider — точная диагностика ошибок валидации."""
