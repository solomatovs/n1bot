"""Application-bootstrap конфигурации поверх boba_next.config.

Содержит:
  * `AppCoreConfig` / `AppCoreSection` — кросс-слойные настройки приложения.
  * `AgentSection` — лимиты агента + tool-фильтры; factory строит DTO `AgentConfig`.
  * `AppConfigBootstrap` — удобный one-shot композитор `ConfigBundleFactory`
    + `AppConfigFactory` для запуска приложения.

Не содержит legacy-обёрток `ChainedConfigResolver`/`ConfigKey`/`DefaultSource` —
конфиг читается через `ConfigPath` + `FlatConfig` (boba_next.config).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.patterns import Always, Never, Specification, StrId
from boba_next.agent.models import AgentConfig
from boba_next.config import (
    AppConfig,
    AppConfigFactory,
    ConfigBundle,
    ConfigBundleFactory,
    ConfigSection,
    ConfigSource,
)
from boba_next.declaration import FieldSpec, ObjectSchema
from boba_next.tools import Tool, ToolNameIn, ToolSourceIn
from boba_next.validators import (
    ChainConverter,
    Default,
    MinValue,
    Nullable,
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
)

__all__ = [
    "AgentSection",
    "AppConfigBootstrap",
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


# ─────────────── AppConfigBootstrap (one-shot композитор) ───────────────


class AppConfigBootstrap:
    """Удобная one-shot обёртка: накопить sources + секций → AppConfig.

    Композирует две foundation-фабрики:
      * `ConfigBundleFactory` — sources → ConfigBundle (FoldFactory).
      * `AppConfigFactory`    — sections → AppConfig (ContextCatalogFactory).

    Использование при старте приложения:

        boot = AppConfigBootstrap()
        boot.attach_sources([toml_source, env_source, cli_source])
        boot.register_section(AppCoreSection())
        boot.register_section(AgentSection())
        boot.discover_extension_sections()
        app: AppConfig = boot.build()
    """

    def __init__(self) -> None:
        self._bundle_factory = ConfigBundleFactory()
        self._app_factory = AppConfigFactory()

    def attach_sources(self, sources: Iterable[ConfigSource]) -> None:
        self._bundle_factory.attach_sources(sources)

    def register_section(self, section: ConfigSection[Any]) -> None:
        self._app_factory.register_section(section)

    def discover_extension_sections(self) -> None:
        self._app_factory.discover_extension_sections()

    def bundle_factory(self) -> ConfigBundleFactory:
        return self._bundle_factory

    def app_factory(self) -> AppConfigFactory:
        return self._app_factory

    def build_bundle(self) -> ConfigBundle:
        """Собрать только foundation-bundle (без секций)."""
        return self._bundle_factory.build()

    def build(self) -> AppConfig:
        """Собрать готовый AppConfig: sources → bundle → секции в реестр."""
        return self._app_factory.build(self._bundle_factory.build())
