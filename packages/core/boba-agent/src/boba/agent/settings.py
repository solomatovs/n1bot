"""Модели файла настроек агента `settings.json`: один слой и его части.

Файл — API процесса агента: его пишет harness (чат) и пользователь, читает
вход процесса (boba-cli), который сливает слои в действующие настройки.
Harness моделей агента не импортирует: форму файла он сверяет с JSON Schema,
которую процесс печатает командой `boba settings-schema` (SettingsSchema).

Ошибки: своих не выпускает; разбор — pydantic.ValidationError.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)
from pydantic.alias_generators import to_camel
from pydantic.json_schema import models_json_schema

from boba.agent.profile import CompactionSettings, TurnLimits
from boba.agent.records import PermissionMode, ReasoningReturn
from boba.core.secrets import SecretRevealing

__all__ = [
    "CompactionJson",
    "HistoryKind",
    "HistoryPoolSettings",
    "HistorySettings",
    "JsonlHistorySettings",
    "LimitsJson",
    "McpClientJson",
    "MemoryHistorySettings",
    "ModelSettings",
    "PermissionSettings",
    "PostgresHistorySettings",
    "ProviderKind",
    "ProviderSettings",
    "SecretKey",
    "SessionsJson",
    "SettingsLayer",
    "SettingsModel",
    "SettingsSchema",
    "ToolsJson",
]


class ProviderKind(StrEnum):
    """Виды провайдеров модели, которые умеет собрать вход процесса."""

    OPENAI = "openai"
    OLLAMA = "ollama"
    ONNX = "onnx"

    @property
    def remote(self) -> bool:
        return self is not ProviderKind.ONNX


class HistoryKind(StrEnum):
    """Виды хранилища истории: файлы, Postgres, только память процесса."""

    JSONL = "jsonl"
    POSTGRES = "postgres"
    MEMORY = "memory"


class SecretKey(StrEnum):
    """Ключи файла настроек, под которыми лежат секреты."""

    PROVIDER = "provider"
    API_KEY = "apiKey"
    DSN = "dsn"


class SettingsModel(SecretRevealing):
    """Основа моделей файла настроек: ключи файлов — camelCase (имена полей
    тоже принимаются), лишние ключи — ошибка; revealed() даёт дамп с
    открытыми секретами для записи файла (SecretRevealing ядра)."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )


class ProviderSettings(SettingsModel):
    """Провайдер модели: вид, адрес, ключ значением либо именем переменной,
    для локальной модели — каталог весов; у удалённого — поведение запросов:
    срок, проверка TLS, повторы и таблица транспорта процесса.

    Таблица `transport` лежит как есть, ключами HttpTransportConfig
    (snake_case): её проверяет модель провайдера при сборке профиля, как и
    `sampling` уходит в запрос без разбора здесь."""

    kind: ProviderKind
    base_url: str | None = None
    api_key: SecretStr | None = None
    api_key_env: str | None = Field(default=None, min_length=1)
    model_dir: str | None = None
    timeout_sec: float = Field(default=120.0, gt=0)
    ssl_verify: bool = True
    retry_attempts: int = Field(default=1, ge=1)
    retry_backoff_sec: float = Field(default=1.0, ge=0)
    retry_statuses: Mapping[int, int] = Field(default_factory=dict)
    retry_after_max_sec: float = Field(default=60.0, ge=0)
    transport: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_key_source(self) -> ProviderSettings:
        by_value = self.api_key is not None
        by_variable = self.api_key_env is not None
        if by_value and by_variable:
            msg = (
                "'apiKey' and 'apiKeyEnv' are both set; keep one: the key itself "
                "in a file with mode 0600, or the name of the variable that holds it."
            )
            raise ValueError(msg)

        return self


class ModelSettings(SettingsModel):
    """Ключ `models.<имя>`: профиль сессии — всё, что ядро знает о модели
    (ModelProfile), и свой системный промпт `systemPrompt`: сессия на этом
    профиле берёт его вместо промпта настроек (systemPromptFile), смена
    профиля в сессии меняет и промпт."""

    provider: ProviderSettings
    model: str = Field(min_length=1)
    sampling: dict[str, Any] = Field(default_factory=dict)
    context_window: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    chars_per_token: int = Field(default=3, gt=0)
    supports_tools: bool = True
    supports_images: bool = False
    system_turns: bool = False
    reasoning_return: ReasoningReturn = ReasoningReturn.NEVER
    system_prompt: str | None = None


class PermissionSettings(SettingsModel):
    """Ключ `permissions`: режим по умолчанию, право на обход, правила."""

    default_mode: PermissionMode | None = None
    allow_bypass: bool | None = None
    allow: Sequence[str] = ()
    deny: Sequence[str] = ()


class JsonlHistorySettings(SettingsModel):
    kind: Literal[HistoryKind.JSONL]
    root: str = Field(min_length=1)


class HistoryPoolSettings(SettingsModel):
    """Ключ `history.pool`: пул соединений истории, общий для всех сессий
    процесса, — сколько держать открытыми, сколько открывать самое большее
    и сколько ждать свободного соединения. Сверх пула процесс держит одно
    соединение замков сессий."""

    min_size: int = Field(ge=1)
    max_size: int = Field(ge=1)
    timeout_sec: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> HistoryPoolSettings:
        if self.max_size < self.min_size:
            msg = (
                f"history.pool.maxSize ({self.max_size}) must be at least "
                f"history.pool.minSize ({self.min_size})"
            )
            raise ValueError(msg)

        return self


class PostgresHistorySettings(SettingsModel):
    """Журнал в Postgres: строка подключения libpq значением либо именем
    переменной окружения, таблица — в схеме из обязательного ключа `schema`,
    пул соединений — ключом `pool`.
    `owner` — логин пользователя, от имени которого работает процесс: его
    ставит служба агента, и процесс видит только сессии этого пользователя;
    без ключа (запуск по stdio) процессу видны все сессии."""

    kind: Literal[HistoryKind.POSTGRES]
    dsn: SecretStr | None = None
    dsn_env: str | None = Field(default=None, min_length=1)
    db_schema: str = Field(min_length=1, alias="schema")
    pool: HistoryPoolSettings
    owner: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_dsn_source(self) -> PostgresHistorySettings:
        by_value = self.dsn is not None
        by_variable = self.dsn_env is not None
        if not by_value and not by_variable:
            msg = (
                "history of kind 'postgres' needs 'dsn' with the libpq connection "
                "string or 'dsnEnv' with the name of the variable that holds it."
            )
            raise ValueError(msg)

        if by_value and by_variable:
            msg = (
                "'dsn' and 'dsnEnv' are both set; keep one: the connection string "
                "itself in a file with mode 0600, or the name of the variable."
            )
            raise ValueError(msg)

        return self


class MemoryHistorySettings(SettingsModel):
    """Журнал только в памяти процесса: после перезапуска истории нет."""

    kind: Literal[HistoryKind.MEMORY]


HistorySettings = Annotated[
    JsonlHistorySettings | PostgresHistorySettings | MemoryHistorySettings,
    Field(discriminator="kind"),
]


class CompactionJson(SettingsModel):
    compact_at: float | None = Field(default=None, gt=0, lt=1)
    block_at: float | None = Field(default=None, gt=0, le=1)

    def core(self) -> CompactionSettings:
        values = self.model_dump(exclude_none=True, by_alias=False)

        return CompactionSettings.model_validate(values)


class LimitsJson(SettingsModel):
    max_turns: int | None = Field(default=None, gt=0)
    tool_result_max_chars: int | None = Field(default=None, gt=0)
    tool_result_window_share: float | None = Field(default=None, gt=0, le=1)
    api_retries: int | None = Field(default=None, ge=0)
    output_limit_retries: int | None = Field(default=None, ge=0)

    def core(self) -> TurnLimits:
        values = self.model_dump(exclude_none=True, by_alias=False)

        return TurnLimits.model_validate(values)


class SessionsJson(SettingsModel):
    """Ключ `sessions`: сколько сессий процесс держит открытыми разом —
    у каждой свой поток; сверх предела открытие сессии отказывает."""

    max_open: int = Field(default=64, gt=0)


class McpClientJson(SettingsModel):
    """Ключ `mcp`: сроки ожидания серверов MCP-over-ACP — одной страницы
    `tools/list` и одного вызова `tools/call`, — и сколько раз сервер может
    попросить ввод пользователя (input_required) в одном вызове."""

    list_timeout_sec: float = Field(default=30.0, gt=0)
    call_timeout_sec: float = Field(default=600.0, gt=0)
    input_rounds: int = Field(default=3, gt=0)


class ToolsJson(SettingsModel):
    """Ключ `tools`: инструменты, которые модель получает полным определением
    с первого запроса, — полные имена, как их видит модель
    (`mcp__<сервер>__<инструмент>`). Остальные инструменты серверов отложены:
    модель видит их имена и загружает ToolSearch. Пустой список — всё через
    ToolSearch."""

    always_load: Sequence[str]


class SettingsLayer(SettingsModel):
    """Один слой `settings.json`; каждый ключ необязателен. Профили `models`
    лежат сырыми таблицами: слой вправе дополнить профиль одним полем, а
    целиком профиль проверяется после слияния."""

    model: str | None = None
    models: Mapping[str, Mapping[str, Any]] = Field(default_factory=dict)
    permissions: PermissionSettings | None = None
    history: HistorySettings | None = None
    compaction: CompactionJson | None = None
    limits: LimitsJson | None = None
    mcp: McpClientJson | None = None
    tools: ToolsJson | None = None
    sessions: SessionsJson | None = None
    env: Mapping[str, str] = Field(default_factory=dict)
    system_prompt_file: str | None = None
    skills_dir: str | None = None
    agent_name: str | None = None

    def secrets(self) -> Iterator[str]:
        """Ключи слоя, под которыми лежат секреты значениями: по ним решается,
        откуда слой разрешено читать."""
        history = self.history
        in_postgres = isinstance(history, PostgresHistorySettings)
        if in_postgres and history.dsn is not None:
            yield f"history.{SecretKey.DSN.value}"

        for name, raw in self.models.items():
            provider = raw.get(SecretKey.PROVIDER.value)
            if not isinstance(provider, Mapping):
                continue

            if provider.get(SecretKey.API_KEY.value) is None:
                continue

            yield f"models.{name}.{SecretKey.PROVIDER.value}.{SecretKey.API_KEY.value}"


class SettingsSchema:
    """JSON Schema одного слоя `settings.json`, которую процесс публикует
    командой `boba settings-schema`: слой целиком, профили `models` —
    полными ModelSettings. По ней harness сверяет файл, который пишет своей
    моделью, не импортируя моделей агента. Создаёт вход процесса (boba-cli)."""

    DIALECT: ClassVar[str] = "https://json-schema.org/draft/2020-12/schema"
    TITLE: ClassVar[str] = "boba settings.json layer"
    REF_TEMPLATE: ClassVar[str] = "#/$defs/{model}"
    DEFS: ClassVar[str] = "$defs"

    def document(self) -> dict[str, Any]:
        refs, shared = models_json_schema(
            [(SettingsLayer, "validation"), (ModelSettings, "validation")],
            ref_template=self.REF_TEMPLATE,
        )
        definitions: dict[str, Any] = shared[self.DEFS]
        layer: dict[str, Any] = definitions[SettingsLayer.__name__]
        layer["properties"]["models"]["additionalProperties"] = refs[
            (ModelSettings, "validation")
        ]

        return {
            "$schema": self.DIALECT,
            "title": self.TITLE,
            **refs[(SettingsLayer, "validation")],
            self.DEFS: definitions,
        }
