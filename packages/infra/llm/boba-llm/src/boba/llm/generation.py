"""Генерация ответа по json-схеме: union-конфиг, локальный ONNX и openai-бэкенды.

Ошибки:
GenerationError — модель не загрузилась, провайдер недоступен либо вернул тело
    не по контракту chat/completions.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import threading
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from boba.llm.openai import OpenAiConfig
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)

__all__ = [
    "GenerationConfig",
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


class OnnxModel(Protocol):
    """Загруженная модель onnxruntime-genai."""


class OnnxTokenizer(Protocol):
    """Токенайзер модели и шаблон диалога при нём."""

    @abstractmethod
    def encode(self, text: str) -> Sequence[int]: ...

    @abstractmethod
    def decode(self, tokens: Sequence[int]) -> str: ...

    @abstractmethod
    def apply_chat_template(
        self,
        messages: str,
        *,
        add_generation_prompt: bool,
    ) -> str: ...


class OnnxParams(Protocol):
    """Параметры прогона: поиск и грамматика ответа."""

    @abstractmethod
    def set_search_options(self, **options: object) -> None: ...

    @abstractmethod
    def set_guidance(self, kind: str, data: str) -> None: ...


class OnnxGenerator(Protocol):
    """Пошаговая генерация одной последовательности."""

    @abstractmethod
    def append_tokens(self, tokens: Sequence[int]) -> None: ...

    @abstractmethod
    def generate_next_token(self) -> None: ...

    @abstractmethod
    def is_done(self) -> bool: ...

    @abstractmethod
    def get_sequence(self, index: int) -> Sequence[int]: ...


class OnnxGenai:
    """Вход в onnxruntime-genai: библиотека идёт без аннотаций, поэтому её
    объекты разбираются здесь один раз и дальше живут протоколами."""

    MODULE: ClassVar[str] = "onnxruntime_genai"

    def __init__(self) -> None:
        imports = Elapsed()
        try:
            self._module = importlib.import_module(self.MODULE)
        except ImportError as exc:
            msg = f"{self.MODULE} is not installed"
            raise GenerationError(msg) from exc

        logger.info("generator: %s imported in %dms", self.MODULE, imports.ms())

    def load(self, model_dir: str) -> tuple[OnnxModel, OnnxTokenizer]:
        try:
            model = self._module.Model(self._module.Config(model_dir))
            tokenizer = self._module.Tokenizer(model)
        except Exception as exc:
            msg = f"model not loaded: {model_dir}"
            raise GenerationError(msg) from exc

        return model, tokenizer

    def params(self, model: OnnxModel) -> OnnxParams:
        return self._module.GeneratorParams(model)

    def generator(self, model: OnnxModel, params: OnnxParams) -> OnnxGenerator:
        return self._module.Generator(model, params)


class LocalOnnxGenerator(StructuredGenerator):
    """Генерация in-process на onnxruntime-genai; формат держит грамматика llguidance.

    Схема компилируется в грамматику и ограничивает сэмплинг, поэтому ответ
    синтаксически верен по построению — вызовы функций тут не нужны.

    Модель одна на процесс, поэтому прогон уходит в поток под локом: ONNX и так
    занимает все доступные ядра, а loop остаётся свободен.
    """

    def __init__(self, cfg: LocalGeneration) -> None:
        runtime = OnnxGenai()

        # размер пула onnxruntime берёт из маски доступных ядер, а её
        # выставляет запуск по cgroup-квоте профиля
        cores = len(os.sched_getaffinity(0))
        logger.info("generator: %s on %d core(s)", cfg.model_dir, cores)

        load = Elapsed()
        model, tokenizer = runtime.load(cfg.model_dir)
        logger.info("generator: %s loaded in %dms", cfg.model_dir, load.ms())

        self._runtime = runtime
        self._cfg = cfg
        self._model = model
        self._tokenizer = tokenizer
        self._lock = threading.Lock()

        # захваченный в момент fork замок остался бы захваченным в ребёнке
        # навсегда: владелец в ребёнка не переносится
        os.register_at_fork(after_in_child=self._reset_lock)

    def _reset_lock(self) -> None:
        self._lock = threading.Lock()

    async def generate(self, user: str, schema: SchemaSpec) -> str:
        return await asyncio.to_thread(self._locked_generate, user, schema)

    def _locked_generate(self, user: str, schema: SchemaSpec) -> str:
        with self._lock:
            return self._generate(user, schema)

    def _generate(self, user: str, schema: SchemaSpec) -> str:
        prompt = self._prompt(user)
        encoded = self._tokenizer.encode(prompt)

        params = self._runtime.params(self._model)
        params.set_search_options(
            max_length=len(encoded) + self._cfg.max_tokens,
            do_sample=False,
        )
        params.set_guidance(GuidanceType.JSON_SCHEMA.value, json.dumps(schema.body))

        elapsed = Elapsed()
        try:
            produced = self._decode(params, encoded)
        except Exception as exc:
            msg = f"local generation failed: {self._cfg.model_dir}"
            raise GenerationError(msg) from exc

        logger.info("generator: %d token(s) in %dms", len(produced), elapsed.ms())

        return self._tokenizer.decode(produced)

    def _decode(self, params: OnnxParams, encoded: Sequence[int]) -> Sequence[int]:
        """Токены ответа без промпта: get_sequence отдаёт всю ленту целиком."""
        generator = self._runtime.generator(self._model, params)
        generator.append_tokens(encoded)

        while not generator.is_done():
            generator.generate_next_token()

        return generator.get_sequence(0)[len(encoded) :]

    def _prompt(self, user: str) -> str:
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

        rendered = self._tokenizer.apply_chat_template(
            json.dumps(messages),
            add_generation_prompt=True,
        )

        return f"{rendered}{self._cfg.reply_prefix}"


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
