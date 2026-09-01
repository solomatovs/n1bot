"""Генерация ответа по json-схеме: схема, профили бэкендов, порт генератора.

Реализации (локальный ONNX, openai) живут в boba.llm.generation.

Ошибки:
GenerationError — модель не загрузилась, провайдер недоступен либо вернул тело
    не по контракту; выпускают реализации порта.
"""

from __future__ import annotations

import json
from abc import abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from boba.chat.http import HttpConfig

__all__ = [
    "ChatField",
    "ChatRole",
    "GenerationBase",
    "GenerationConfig",
    "GenerationError",
    "GenerationMessages",
    "GuidanceType",
    "LocalGeneration",
    "OpenAiGeneration",
    "SchemaSpec",
    "StructuredGenerator",
]


class GenerationError(Exception):
    """Структурированный ответ не получен."""


class ChatRole(StrEnum):
    """Роли сообщений диалога."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ChatField(StrEnum):
    """Ключи тела запроса chat/completions."""

    MODEL = "model"
    MESSAGES = "messages"
    ROLE = "role"
    CONTENT = "content"
    TOOLS = "tools"
    TOOL_CHOICE = "tool_choice"
    TYPE = "type"
    FUNCTION = "function"
    NAME = "name"
    DESCRIPTION = "description"
    PARAMETERS = "parameters"
    STREAM = "stream"


class GuidanceType(StrEnum):
    """Виды грамматик llguidance в onnxruntime-genai."""

    JSON_SCHEMA = "json_schema"
    REGEX = "regex"
    LARK = "lark"


class SchemaSpec(BaseModel):
    """Схема ответа: имя, описание и тело json schema.

    Одна и та же спецификация уходит локальной модели грамматикой, а удалённой —
    объявлением функции: формат ответа задаёт схема, а не бэкенд.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Имя схемы; удалённому провайдеру — имя функции.")
    description: str = Field(description="Назначение схемы для модели.")
    body: Mapping[str, Any] = Field(description="Тело json schema.")


class GenerationBase(BaseModel):
    """Общее поле профилей генерации: системный промпт."""

    model_config = ConfigDict(extra="ignore")

    system_prompt: str = Field(description="Системный промпт генерации.")


class GenerationMessages:
    """Json-диалог system+user для chat_template локальной модели."""

    @staticmethod
    def render(system_prompt: str, user: str) -> str:
        messages = [
            {
                ChatField.ROLE.value: ChatRole.SYSTEM.value,
                ChatField.CONTENT.value: system_prompt,
            },
            {
                ChatField.ROLE.value: ChatRole.USER.value,
                ChatField.CONTENT.value: user,
            },
        ]

        return json.dumps(messages, ensure_ascii=False)


class LocalGeneration(GenerationBase):
    """Локальный инференс onnxruntime-genai (in-process, ONNX)."""

    kind: Literal["local"]

    max_tokens: int = Field(
        gt=0,
        description=(
            "Потолок токенов прогона; обязателен. Он же стоп для модели, "
            "ушедшей в повтор. Не поле запроса: тела запроса тут нет."
        ),
    )

    model_dir: str = Field(
        description=(
            "Каталог модели onnxruntime-genai: genai_config.json, веса и "
            "токенайзер. Модель кладётся заранее, из сети ничего не тянется."
        ),
    )

    reply_prefix: str = Field(
        description=(
            "Текст, дописываемый к промпту после метки ответа; обязателен. "
            "Пусто — модель отвечает как есть; у reasoning-моделей семейства "
            "qwen3 сюда идёт пустой блок '<think>\\n\\n</think>\\n\\n', "
            "отключающий размышления."
        ),
    )


class OpenAiGeneration(GenerationBase):
    """Удалённый инференс через openai-совместимый endpoint /chat/completions."""

    kind: Literal["openai"]

    http: HttpConfig = Field(
        description=(
            "Поведение HTTP-транспорта; в конфиге подключается ссылкой ${http}."
        ),
    )

    base_url: str = Field(description="Endpoint API провайдера.")

    api_key: str = Field(description="Ключ API провайдера.")

    model: str = Field(description="Имя модели у провайдера.")

    sampling: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Параметры запроса к провайдеру как есть: имена и значения уходят "
            "в тело запроса без проверок и переименований. Потолок ответа "
            "задаётся здесь же тем именем, которое понимает провайдер."
        ),
    )


GenerationConfig = Annotated[
    LocalGeneration | OpenAiGeneration,
    Field(discriminator="kind"),
]
"""Discriminated union по kind — точная диагностика ошибок валидации."""


class StructuredGenerator(Protocol):
    """Порт генерации по схеме: наружу отдаётся ответ модели как есть."""

    @abstractmethod
    async def generate(self, user: str, schema: SchemaSpec) -> str: ...
