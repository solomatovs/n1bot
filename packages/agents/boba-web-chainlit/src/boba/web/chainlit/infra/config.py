"""DTO/Schema chainlit-приложения: AppCoreConfig + плоский AppConfig."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from boba.agent.orchestrator import AgentConfig
from boba.agent.prompt_providers import PromptsConfig
from boba.agent.workspace_fs import WorkspaceLayout
from boba.coercion import ChainCoercer, Default, Nullable, ParseBool, ParseString
from boba.declaration import FieldSpec, ObjectSchema
from boba.provider.openai import OpenAIConfig

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
    """DTO одной плоской секции `[agent]`. Поля под-DTO лежат рядом."""

    core: AppCoreConfig
    workspaces: WorkspaceLayout
    openai: OpenAIConfig
    prompts: PromptsConfig
    runtime: AgentConfig

    SCHEMA: ClassVar[ObjectSchema[AppConfig]]

    @staticmethod
    def _build(**flat: Any) -> AppConfig:
        return AppConfig(
            core=AppCoreConfig(
                ssl_verify=flat["ssl_verify"],
                log_level=flat["log_level"],
                log_file=flat["log_file"],
            ),
            workspaces=WorkspaceLayout(
                base_dir=flat["base_dir"],
                user_subdir=flat["user_subdir"],
                system_subdir=flat["system_subdir"],
                tmp_subdir=flat["tmp_subdir"],
            ),
            openai=OpenAIConfig(
                base_url=flat["base_url"],
                api_key=flat["api_key"],
            ),
            prompts=PromptsConfig(
                dir=flat["dir"],
            ),
            runtime=AgentConfig(
                max_iterations=flat["max_iterations"],
                max_consecutive_tool_calls=flat["max_consecutive_tool_calls"],
            ),
        )


AppConfig.SCHEMA = ObjectSchema(
    description="Плоский agent-конфиг: core/workspaces/openai/prompts/runtime.",
    fields=[
        *AppCoreConfig.SCHEMA.fields,
        *WorkspaceLayout.SCHEMA.fields,
        *OpenAIConfig.SCHEMA.fields,
        *PromptsConfig.SCHEMA.fields,
        *AgentConfig.SCHEMA.fields,
    ],
    factory=AppConfig._build,
)
