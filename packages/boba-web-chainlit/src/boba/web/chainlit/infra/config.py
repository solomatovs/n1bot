"""Базовые секции приложения: AppCoreSection, AgentSection.

`AppConfigBootstrap` — в `boba.config.bootstrap`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.agent.models import AgentConfig
from boba.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    Nullable,
    ParseBool,
    ParseInt,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId

__all__ = [
    "AgentSection",
    "AppCoreConfig",
    "AppCoreSection",
]


# ─────────────── AppCoreSection ───────────────


@dataclass(frozen=True)
class AppCoreConfig:
    """DTO AppCoreSection — кросс-слойные настройки приложения."""

    ssl_verify: bool
    log_level: str
    log_file: str | None


class AppCoreSection(ConfigSection[AppCoreConfig]):
    """Кросс-слойные настройки приложения: SSL/логирование."""

    id: ClassVar[StrId] = StrId("app_core")
    namespace: ClassVar[tuple[str, ...]] = ("app",)

    schema: ClassVar[ObjectSchema[AppCoreConfig]] = ObjectSchema(
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


# ─────────────── AgentSection ───────────────


class AgentSection(ConfigSection[AgentConfig]):
    """Лимиты агентского лупа. Фильтрация tools — per-ext в [ext.<name>]."""

    namespace: ClassVar[tuple[str, ...]] = ("agent",)

    schema: ClassVar[ObjectSchema[AgentConfig]] = ObjectSchema(
        description="Лимиты агентского лупа.",
        fields=[
            FieldSpec(
                name="max_iterations",
                coercer=ChainCoercer(Default(20), ParseInt(), MinValue(1)),
                description="Жёсткий потолок числа итераций агента в одной сессии.",
            ),
            FieldSpec(
                name="max_consecutive_tool_calls",
                coercer=ChainCoercer(Default(3), ParseInt(), MinValue(1)),
                description=(
                    "Сколько раз подряд агент может звать tools без LLM-ответа."
                ),
            ),
        ],
        factory=AgentConfig,
    )
