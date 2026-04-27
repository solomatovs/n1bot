"""Сборка типизированной конфигурации приложения из секций."""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar, cast

from boba.domain.agent.models import AgentConfig
from boba.domain.core.config import (
    ChainedConfigResolver,
    ConfigKey,
    ConfigSection,
    ConfigSource,
    FieldSpec,
    ObjectSchema,
)
from boba.domain.core.declaration import FieldMissingError
from boba.domain.core.patterns import (
    Always,
    ConverterInputError,
    Never,
    Specification,
    StrId,
)
from boba.domain.core.tools.specs import ToolNameIn, ToolSourceIn
from boba.domain.core.tools.tool import Tool
from boba.domain.core.validators import (
    ChainConverter,
    Default,
    MinValue,
    Nullable,
    ParseBool,
    ParseInt,
    ParseList,
    ParseString,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "CONFIG_SECTIONS_ENTRY_POINT",
    "AgentSection",
    "AppCoreConfig",
    "AppCoreSection",
    "ConfigBundle",
    "ConfigError",
    "ConfigFactory",
    "ConfigSectionAlreadyRegisteredError",
    "ConfigSectionMissingError",
    "DefaultSource",
]


CONFIG_SECTIONS_ENTRY_POINT = "boba.config_sections"
"""Entry-point group для секций расширений."""

class DefaultSource(ConfigSource):
    """Статический fallback-словарь ConfigKey → значение."""

    def __init__(self, defaults: Mapping[ConfigKey, object]) -> None:
        self._defaults = dict(defaults)

    def resolve(self, key: ConfigKey) -> object | None:
        return self._defaults.get(key)

class ConfigError(Exception):
    """Базовая ошибка конфиг-инфры."""


class ConfigSectionAlreadyRegisteredError(ConfigError):
    """Секция с таким StrId уже зарегистрирована."""

    def __init__(self, section_id: StrId) -> None:
        super().__init__(f"ConfigSection {section_id!r} is already registered")
        self.section_id = section_id


class ConfigSectionMissingError(ConfigError):
    """Секция не зарегистрирована в фабрике."""

    def __init__(self, section_cls: type[ConfigSection[Any]]) -> None:
        super().__init__(
            f"ConfigSection {section_cls.__name__!r} (id={section_cls.id!r}) "
            "is not registered in factory"
        )
        self.section_cls = section_cls


@dataclass(frozen=True)
class AppCoreConfig:
    """DTO AppCoreSection."""

    ssl_verify: bool
    log_level: str
    log_file: str | None


class ConfigBundle:
    """Иммутабельный реестр DTO по StrId."""

    def __init__(self, sections: Mapping[StrId, object]) -> None:
        self._sections: dict[StrId, object] = dict(sections)

    def section(self, cls: type[ConfigSection[T]]) -> T:
        """Достать DTO секции; ConfigSectionMissingError если не зарегистрирована."""
        sid = cls.id
        if sid not in self._sections:
            raise ConfigSectionMissingError(cls)
        return cast(T, self._sections[sid])

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
                description="Проверять ли TLS-сертификат у HTTPS-запросов "
                "из приложения.",
            ),
            FieldSpec(
                name="log_level",
                converter=ChainConverter(Default("INFO"), ParseString()),
                description="Уровень корневого логгера: "
                "DEBUG/INFO/WARNING/ERROR/CRITICAL.",
            ),
            FieldSpec(
                name="log_file",
                converter=Nullable(ParseString()),
                description="Путь к log-файлу. Если пусто — логи только в stderr.",
            ),
        ],
        factory=AppCoreConfig,
    )


def _build_agent_config(  # noqa: PLR0913 — kwargs приходят из ObjectSchema, число фиксировано полями секции
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
                description="Жёсткий потолок числа итераций агента "
                "в одной сессии.",
            ),
            FieldSpec(
                name="max_consecutive_tool_calls",
                converter=ChainConverter(Default(3), ParseInt(), MinValue(1)),
                description="Сколько раз подряд агент может звать tools "
                "без LLM-ответа.",
            ),
            FieldSpec(
                name="tools_enabled",
                converter=ChainConverter(Default(True), ParseBool()),
                description="Подключать ли tool-плагины. "
                "false — агент работает без инструментов.",
            ),
            FieldSpec(
                name="tool_plugins",
                converter=ChainConverter(Default([]), ParseList(ParseString())),
                description="Whitelist по entry-point names "
                "(boba.tools). Пусто — все плагины.",
            ),
            FieldSpec(
                name="tools_allow",
                converter=ChainConverter(Default([]), ParseList(ParseString())),
                description="Whitelist по именам tools. Пусто — все.",
            ),
            FieldSpec(
                name="tools_deny",
                converter=ChainConverter(Default([]), ParseList(ParseString())),
                description="Blacklist по именам tools. Применяется "
                "поверх tools_allow.",
            ),
        ],
        factory=_build_agent_config,
    )

class ConfigFactory:
    """Реестр секций и сборщик ConfigBundle."""

    def __init__(self) -> None:
        self._sources: list[ConfigSource] = []
        self._sections: dict[StrId, ConfigSection[Any]] = {}
        self._resolver: ChainedConfigResolver | None = None
        self._bundle: ConfigBundle | None = None

    def attach_sources(self, sources: Sequence[ConfigSource]) -> None:
        """Подключить источники в порядке приоритета; сбрасывает кеш."""
        self._sources = list(sources)
        self._resolver = None
        self._bundle = None

    def _iter_schema_items(self) -> Iterator[tuple[ConfigKey, FieldSpec[Any]]]:
        """Все (key, field) пары по зарегистрированным секциям."""
        for section in self._sections.values():
            for fld in section.schema.fields:
                yield ConfigKey(*section.namespace, fld.name), fld

    def resolver(self) -> ChainedConfigResolver:
        """Лениво собранный резолвер; идемпотентен."""
        if self._resolver is not None:
            return self._resolver
        if not self._sources:
            raise ConfigError(
                "ConfigFactory: no sources attached. "
                "Call attach_sources([...]) before build()/resolver()."
            )
        items = list(self._iter_schema_items())
        for src in self._sources:
            src.bind_schema(items)
        self._resolver = ChainedConfigResolver(self._sources)
        return self._resolver

    def describe_key(self, key: ConfigKey) -> list[str]:
        """Рецепты задания ключа — по одному на источник."""
        out: list[str] = []
        for src in self.resolver().sources:
            d = src.describe(key)
            if d:
                out.append(d)
        return out

    def format_config_error(self, err: ConverterInputError) -> str:
        """Текст ошибки конфига с подсказкой источников."""
        if (
            isinstance(err, FieldMissingError)
            and isinstance(err.key, ConfigKey)
        ):
            hints = self.describe_key(err.key)
            if hints:
                return f"{err}; задайте через: {' / '.join(hints)}"
        return str(err)

    def register(self, section: ConfigSection[Any]) -> None:
        sid = section.id
        if sid in self._sections:
            raise ConfigSectionAlreadyRegisteredError(sid)
        self._sections[sid] = section

    def registered(self) -> Sequence[ConfigSection[Any]]:
        return tuple(self._sections.values())

    def discover_extension_sections(self) -> None:
        """Подхватить секции через entry-point group boba.config_sections."""
        for ep in importlib.metadata.entry_points(group=CONFIG_SECTIONS_ENTRY_POINT):
            try:
                obj = ep.load()
            except Exception as e:
                logger.warning(
                    "config_sections entry-point %r load failed: %s: %s; skipped",
                    ep.name,
                    type(e).__name__,
                    e,
                )
                continue
            if not (isinstance(obj, type) and issubclass(obj, ConfigSection)):
                logger.warning(
                    "config_sections entry-point %r target is not a "
                    "ConfigSection subclass: %r; skipped",
                    ep.name,
                    obj,
                )
                continue
            try:
                self.register(obj())
            except ConfigSectionAlreadyRegisteredError as e:
                logger.warning(
                    "config_sections entry-point %r: %s; skipped",
                    ep.name,
                    e,
                )

    def build(self) -> ConfigBundle:
        """Собрать (или вернуть закешированный) ConfigBundle; идемпотентно."""
        if self._bundle is None:
            resolver = self.resolver()
            built: dict[StrId, object] = {
                sid: section.build(resolver)
                for sid, section in self._sections.items()
            }
            self._bundle = ConfigBundle(built)
        return self._bundle
