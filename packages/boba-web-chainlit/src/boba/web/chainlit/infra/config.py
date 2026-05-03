"""Базовые секции приложения: AppCoreSection, AgentSection.

`AppConfigBootstrap` — в `boba.config.bootstrap`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.agent.models import AgentConfig
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.patterns import StrId
from boba.validators import (
    ChainConverter,
    Default,
    MinValue,
    Nullable,
    ParseBool,
    ParseInt,
    ParseString,
)

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
                converter=ChainConverter(Default(False), ParseBool()),
                description="Проверять ли TLS-сертификат у HTTPS-запросов.",
            ),
            FieldSpec(
                name="log_level",
                converter=ChainConverter(Default("INFO"), ParseString()),
                description="Уровень корневого логгера.",
            ),
            FieldSpec(
                name="log_file",
                converter=Nullable(ParseString()),
                description="Путь к log-файлу. Пусто — логи в stderr.",
            ),
        ],
        factory=AppCoreConfig,
    )


# ─────────────── AgentSection ───────────────


class AgentSection(ConfigSection[AgentConfig]):
    """Лимиты агентского лупа. Фильтрация tools — per-ext в [ext.<name>]."""

    id: ClassVar[StrId] = StrId("agent")
    namespace: ClassVar[tuple[str, ...]] = ("agent",)

    schema: ClassVar[ObjectSchema[AgentConfig]] = ObjectSchema(
        description="Лимиты агентского лупа.",
        fields=[
            FieldSpec(
                name="max_iterations",
                converter=ChainConverter(Default(20), ParseInt(), MinValue(1)),
                description="Жёсткий потолок числа итераций агента в одной сессии.",
            ),
            FieldSpec(
                name="max_consecutive_tool_calls",
                converter=ChainConverter(Default(3), ParseInt(), MinValue(1)),
                description=(
                    "Сколько раз подряд агент может звать tools без LLM-ответа."
                ),
            ),
        ],
        factory=AgentConfig,
    )
