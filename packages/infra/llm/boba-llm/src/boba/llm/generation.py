"""Генерация ответа по json-схеме: union-конфиг, локальный ONNX и openai-бэкенды.

Локальный бэкенд работает поверх общего рантайма OnnxChatRuntime (boba.llm.local):
одна загруженная модель обслуживает и чат, и генерацию по схеме.

Ошибки:
GenerationError — модель не загрузилась, провайдер недоступен либо вернул тело
    не по контракту chat/completions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from boba.llm.local import OnnxChatRuntime, RunSpec
from boba.llm.openai import OpenAiConfig
from boba.llm.provider import ChatProviderError
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)

__all__ = [
    "GenerationConfig",
    "GeneratorFactory",
    "GenerationError",
    "LocalGeneration",
    "LocalOnnxGenerator",
    "OpenAiGeneration",
    "OpenAiStructuredGenerator",
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
    TEMPERATURE = "temperature"
    MAX_TOKENS = "max_tokens"
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
    """Общие поля профилей генерации: промпт и потолок ответа."""

    model_config = ConfigDict(extra="ignore")

    system_prompt: str = Field(description="Системный промпт генерации.")

    max_tokens: int = Field(
        gt=0,
        description=(
            "Потолок токенов ответа; обязателен. Локально это ещё и стоп для "
            "модели, ушедшей в повтор."
        ),
    )


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

    provider: Literal["local"]

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

    provider: Literal["openai"]

    openai: OpenAiConfig = Field(
        description=(
            "Транспорт openai-провайдера; в конфиге подключается ссылкой "
            "${openai.<name>}."
        ),
    )

    model: str = Field(description="Имя модели у провайдера.")

    temperature: float = Field(
        ge=0,
        description="Температура сэмплинга; обязательна.",
    )


GenerationConfig = Annotated[
    LocalGeneration | OpenAiGeneration,
    Field(discriminator="provider"),
]
"""Discriminated union по provider — точная диагностика ошибок валидации."""


class StructuredGenerator(Protocol):
    """Порт генерации по схеме: наружу отдаётся ответ модели как есть."""

    @abstractmethod
    async def generate(self, user: str, schema: SchemaSpec) -> str: ...


class LocalOnnxGenerator(StructuredGenerator):
    """Генерация по схеме на общем локальном рантайме OnnxChatRuntime.

    Формат ответа держит грамматика llguidance: схема компилируется в неё и
    ограничивает сэмплинг, поэтому ответ синтаксически верен по построению.
    """

    def __init__(self, cfg: LocalGeneration, runtime: OnnxChatRuntime) -> None:
        self._cfg = cfg
        self._runtime = runtime

    async def generate(self, user: str, schema: SchemaSpec) -> str:
        return await asyncio.to_thread(self._generate, user, schema)

    def _generate(self, user: str, schema: SchemaSpec) -> str:
        rendered = self._runtime.render(
            GenerationMessages.render(self._cfg.system_prompt, user)
        )
        prompt = f"{rendered}{self._cfg.reply_prefix}"

        spec = RunSpec(
            max_tokens=self._cfg.max_tokens,
            guidance_kind=GuidanceType.JSON_SCHEMA.value,
            guidance_data=json.dumps(dict(schema.body)),
        )

        pieces: list[str] = []
        try:
            self._runtime.run(prompt, spec, pieces.append, self._never_stopped)
        except ChatProviderError as exc:
            msg = f"local generation failed: {self._cfg.model_dir}"
            raise GenerationError(msg) from exc

        return "".join(pieces)

    @staticmethod
    def _never_stopped() -> bool:
        """Прогон генерации по схеме не прерывается снаружи."""
        return False


class ChatFunctionCall(BaseModel):
    """Вызов функции в ответе провайдера; arguments — json строкой."""

    model_config = ConfigDict(extra="ignore")

    name: str
    arguments: str


class ChatToolCall(BaseModel):
    """Элемент tool_calls ответа."""

    model_config = ConfigDict(extra="ignore")

    function: ChatFunctionCall


class ChatReplyMessage(BaseModel):
    """Сообщение ассистента: текст и вызовы функций."""

    model_config = ConfigDict(extra="ignore")

    content: str = ""
    tool_calls: Sequence[ChatToolCall] = ()

    @field_validator("content", mode="before")
    @classmethod
    def _text(cls, value: object) -> str:
        """Провайдер шлёт null при вызове функции, а части сообщения — списком."""
        if isinstance(value, str):
            return value

        return ""


class ChatChoice(BaseModel):
    """Вариант ответа провайдера."""

    model_config = ConfigDict(extra="ignore")

    message: ChatReplyMessage


class ChatCompletion(BaseModel):
    """Тело ответа /chat/completions; лишние поля провайдера игнорируются."""

    model_config = ConfigDict(extra="ignore")

    choices: Sequence[ChatChoice]


class OpenAiStructuredGenerator(StructuredGenerator):
    """Генерация через openai-совместимый /chat/completions.

    Схема уходит объявлением функции: response_format роутеры отклоняют.
    Ответ отдаётся наружу как есть — строкой аргументов вызова либо текстом
    сообщения: что из этого пришло, знает разбирающий, а не транспорт.
    """

    ENDPOINT: ClassVar[str] = "chat/completions"

    def __init__(self, cfg: OpenAiGeneration, client: httpx.AsyncClient) -> None:
        self._cfg = cfg
        self._client = client

    async def generate(self, user: str, schema: SchemaSpec) -> str:
        payload = self._payload(user, schema)
        headers = {"Authorization": f"Bearer {self._cfg.openai.api_key}"}

        elapsed = Elapsed()
        try:
            response = await self._client.post(
                self._endpoint(),
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            msg = f"chat endpoint failed: {exc}"
            raise GenerationError(msg) from exc

        reply = self._parse(response.content)

        logger.info("generator: %s replied in %dms", self._cfg.model, elapsed.ms())

        return self._candidate(reply)

    def _endpoint(self) -> str:
        return self._cfg.openai.base_url.rstrip("/") + "/" + self.ENDPOINT

    def _payload(self, user: str, schema: SchemaSpec) -> dict[str, Any]:
        function = {
            ChatField.NAME.value: schema.name,
            ChatField.DESCRIPTION.value: schema.description,
            ChatField.PARAMETERS.value: dict(schema.body),
        }

        messages = [
            {
                ChatField.ROLE.value: ChatRole.SYSTEM.value,
                ChatField.CONTENT.value: self._cfg.system_prompt,
            },
            {
                ChatField.ROLE.value: ChatRole.USER.value,
                ChatField.CONTENT.value: user,
            },
        ]

        choice = {
            ChatField.TYPE.value: ChatField.FUNCTION.value,
            ChatField.FUNCTION.value: {ChatField.NAME.value: schema.name},
        }

        return {
            ChatField.MODEL.value: self._cfg.model,
            ChatField.MESSAGES.value: messages,
            ChatField.TOOLS.value: [
                {
                    ChatField.TYPE.value: ChatField.FUNCTION.value,
                    ChatField.FUNCTION.value: function,
                }
            ],
            ChatField.TOOL_CHOICE.value: choice,
            ChatField.TEMPERATURE.value: self._cfg.temperature,
            ChatField.MAX_TOKENS.value: self._cfg.max_tokens,
            ChatField.STREAM.value: False,
        }

    @staticmethod
    def _parse(body: bytes) -> ChatCompletion:
        try:
            return ChatCompletion.model_validate_json(body)
        except ValidationError as exc:
            msg = f"chat endpoint returned malformed body: {exc}"
            raise GenerationError(msg) from exc

    @staticmethod
    def _candidate(reply: ChatCompletion) -> str:
        """Строка-кандидат ответа: аргументы вызова либо текст сообщения."""
        if not reply.choices:
            msg = "chat endpoint returned no choices"
            raise GenerationError(msg)

        message = reply.choices[0].message
        if message.tool_calls:
            return message.tool_calls[0].function.arguments

        return message.content


class GeneratorFactory:
    """Собирает StructuredGenerator по union-конфигу.

    Ресурсы приходят снаружи: httpx-клиент openai-бэкенда и локальный рантайм
    строит и держит DI приложения — фабрика только выбирает реализацию.
    """

    @classmethod
    def build(
        cls,
        cfg: LocalGeneration | OpenAiGeneration,
        *,
        client: httpx.AsyncClient | None,
        runtime: OnnxChatRuntime | None,
    ) -> StructuredGenerator:
        match cfg:
            case LocalGeneration():
                if runtime is None:
                    msg = f"local generation needs a runtime: {cfg.model_dir}"
                    raise ValueError(msg)

                return LocalOnnxGenerator(cfg, runtime)
            case OpenAiGeneration():
                if client is None:
                    msg = "openai generation needs an httpx client"
                    raise ValueError(msg)

                return OpenAiStructuredGenerator(cfg, client)
