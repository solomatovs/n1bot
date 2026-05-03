"""Базовые секции приложения: AppCoreSection, AgentSection.

`AppConfigBootstrap` — в `boba.config.bootstrap`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from boba.agent.models import AgentConfig
from boba.config import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.tools import Tool, ToolNameIn, ToolSourceIn
from boba.validators import (
    ChainConverter,
    Default,
    MinValue,
    Nullable,
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
)

from boba.patterns import Always, Never, Specification, StrId

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


def _build_agent_config(  # noqa: PLR0913 — kwargs приходят из ObjectSchema
    max_iterations: int,
    max_consecutive_tool_calls: int,
    tools_enabled: bool,
    tool_plugins: list[str],
    tools_allow: list[str],
    tools_deny: list[str],
) -> AgentConfig:
    """Свернуть TOML-поля в AgentConfig с одним tool_spec."""
    if not tools_enabled:
        return AgentConfig(
            max_iterations=max_iterations,
            max_consecutive_tool_calls=max_consecutive_tool_calls,
            tool_spec=Never[Tool[Any]](),
        )
    spec: Specification[Tool[Any]] = Always[Tool[Any]]()
    if tool_plugins:
        spec = spec.and_(ToolSourceIn(tool_plugins))
    if tools_allow:
        spec = spec.and_(ToolNameIn(tools_allow))
    if tools_deny:
        spec = spec.and_(ToolNameIn(tools_deny).not_())
    return AgentConfig(
        max_iterations=max_iterations,
        max_consecutive_tool_calls=max_consecutive_tool_calls,
        tool_spec=spec,
    )


class AgentSection(ConfigSection[AgentConfig]):
    """Лимиты агентского лупа и фильтрация tool-плагинов."""

    id: ClassVar[StrId] = StrId("agent")
    namespace: ClassVar[tuple[str, ...]] = ("agent",)

    schema: ClassVar[ObjectSchema[AgentConfig]] = ObjectSchema(
        description="Лимиты агентского лупа и фильтрация tool-плагинов.",
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
            FieldSpec(
                name="tools_enabled",
                converter=ChainConverter(Default(True), ParseBool()),
                description="Подключать ли tool-плагины.",
            ),
            FieldSpec(
                name="tool_plugins",
                converter=ChainConverter(Default([]), ParseCsvList()),
                description="Whitelist по entry-point names (boba.tools).",
            ),
            FieldSpec(
                name="tools_allow",
                converter=ChainConverter(Default([]), ParseCsvList()),
                description="Whitelist по именам tools. Пусто — все.",
            ),
            FieldSpec(
                name="tools_deny",
                converter=ChainConverter(Default([]), ParseCsvList()),
                description="Blacklist по именам tools (поверх tools_allow).",
            ),
        ],
        factory=_build_agent_config,
    )


