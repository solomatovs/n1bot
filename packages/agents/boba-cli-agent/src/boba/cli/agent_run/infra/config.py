"""DTO/Schema приложения: AppCoreConfig + плоский AppConfig."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, ClassVar

from boba.agent.orchestrator import AgentConfig
from boba.agent.prompt_providers import PromptsConfig
from boba.agent.workspace_fs import WorkspaceLayout
from boba.provider.openai import OpenAIConfig
from boba.schema import Inline, schema_from_dataclass
from boba.schema.coercion import ChainCoercer, Default, Nullable, ParseBool, ParseString
from boba.schema.declaration import FieldSpec, ObjectSchema

__all__ = ["AppConfig", "AppCoreConfig"]


@dataclass(frozen=True)
class AppCoreConfig:
    """DTO ядра — кросс-слойные настройки приложения."""

    ssl_verify: bool
    log_level: str
    log_file: str | None

    SCHEMA: ClassVar[ObjectSchema[AppCoreConfig]]


AppCoreConfig.SCHEMA = ObjectSchema(
    description="Кросс-слойные настройки приложения: SSL/логирование.",
    fields=[
        FieldSpec(
            name="ssl_verify",
            coercer=ChainCoercer(Default(False), ParseBool()),
            description="Проверять ли TLS-сертификат у HTTPS-запросов.",
        ),
        FieldSpec(
            name="log_level",
            coercer=ChainCoercer(Default("INFO"), ParseString()),
            description="Уровень корневого логгера.",
        ),
        FieldSpec(
            name="log_file",
            coercer=Nullable(ParseString()),
            description="Путь к log-файлу. Пусто — логи в stderr.",
        ),
    ],
    factory=AppCoreConfig,
)


@dataclass(frozen=True)
class AppConfig:
    """Плоский agent-конфиг: core/workspaces/openai/prompts/runtime."""

    core:       Annotated[AppCoreConfig,  Inline()]
    workspaces: Annotated[WorkspaceLayout, Inline()]
    openai:     Annotated[OpenAIConfig,    Inline()]
    prompts:    Annotated[PromptsConfig,   Inline()]
    runtime:    Annotated[AgentConfig,     Inline()]

    SCHEMA: ClassVar[ObjectSchema[AppConfig]]


AppConfig.SCHEMA = schema_from_dataclass(AppConfig)
