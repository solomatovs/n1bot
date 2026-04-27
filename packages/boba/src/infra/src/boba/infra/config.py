"""Сборка типизированной конфигурации приложения из секций.

- ConfigSection декларирует поля через FieldSpec и строит DTO.
- ConfigFactory регистрирует встроенные секции и подхватывает секции
  расширений через entry-point group boba.config_sections; build()
  идемпотентно возвращает ConfigBundle (внутренне кешируется).
- ConfigBundle — generic-реестр DTO по StrId + типизированный section();
  никаких app/agent shortcuts'ов — composition специфичных AppConfig/
  AgentConfig'ов под каждое приложение делает consumer (он знает свои
  адаптерные секции, infra про них не знает).

LLM-sampling-параметров здесь нет — sampling прокидывается caller'ом per-request.
"""

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
from boba.domain.core.patterns import ConverterInputError, StrId
from boba.domain.core.validators import (
    ChainConverter,
    Default,
    MinValue,
    Nullable,
    ParseBool,
    ParseInt,
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
"""Entry-point group для discovery секций расширений."""


# ──────────────────────────────────────────────────────────────────────
# Generic-purpose source: static fallback dict (test/preset helper)
# ──────────────────────────────────────────────────────────────────────


class DefaultSource(ConfigSource):
    """Статический fallback-словарь — для тестов и кастомных пресетов.

    Принимает ConfigKey → значение. Возвращает значение по точному
    совпадению ключа; неизвестные ключи — None. Обычно ставят последним
    в цепочке, чтобы он отрабатывал, только когда «настоящие» источники
    промолчали.
    """

    def __init__(self, defaults: Mapping[ConfigKey, object]) -> None:
        self._defaults = dict(defaults)

    def resolve(self, key: ConfigKey) -> object | None:
        return self._defaults.get(key)


# ──────────────────────────────────────────────────────────────────────
# Bundle and errors
# ──────────────────────────────────────────────────────────────────────


class ConfigError(Exception):
    """Базовая ошибка конфиг-инфры — отделяет сбои фабрики/бандла от
    ошибок-резолверов (ConverterInputError и потомков).
    """


class ConfigSectionAlreadyRegisteredError(ConfigError):
    """Попытка зарегистрировать вторую секцию с тем же StrId."""

    def __init__(self, section_id: StrId) -> None:
        super().__init__(f"ConfigSection {section_id!r} is already registered")
        self.section_id = section_id


class ConfigSectionMissingError(ConfigError):
    """Запрошен bundle.section(SectionCls), но секция не зарегистрирована.

    Это инвариант сборки фабрики: секция должна быть зарегистрирована до
    build. Для встроенных секций гарантируется
    default_config_factory; для секций расширений — discovery через
    entry-point group boba.config_sections.
    """

    def __init__(self, section_cls: type[ConfigSection[Any]]) -> None:
        super().__init__(
            f"ConfigSection {section_cls.__name__!r} (id={section_cls.id!r}) "
            "is not registered in factory"
        )
        self.section_cls = section_cls


@dataclass(frozen=True)
class AppCoreConfig:
    """Внутренний DTO AppCoreSection.

    Не часть публичного API — AppConfig агрегирует поля плоско,
    эта структура нужна только чтобы build имела
    типизированный return-type, симметричный остальным секциям.
    """

    ssl_verify: bool
    log_level: str
    log_file: str | None


class ConfigBundle:
    """Итог сборки: типизированный реестр DTO по StrId.

    section() достаёт DTO нужной секции по её классу — type-checker
    видит конкретный T_DTO. Generic-реестр: никакие приложение-
    специфичные аггрегаты (AppConfig/AgentConfig) тут не живут —
    каждое приложение собирает свой агрегат сам, потому что только
    оно знает, какие adapter-секции зарегистрированы.

    Бандл иммутабельный (внутренний dict копируется при создании).
    """

    def __init__(self, sections: Mapping[StrId, object]) -> None:
        self._sections: dict[StrId, object] = dict(sections)

    def section(self, cls: type[ConfigSection[T]]) -> T:
        """Достать DTO секции cls. Бросает
        ConfigSectionMissingError, если секция не была
        зарегистрирована в фабрике.
        """
        sid = cls.id
        if sid not in self._sections:
            raise ConfigSectionMissingError(cls)
        return cast(T, self._sections[sid])


# ──────────────────────────────────────────────────────────────────────
# Built-in sections
# ──────────────────────────────────────────────────────────────────────


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


class AgentSection(ConfigSection[AgentConfig]):
    """Лимиты агентского лупа."""

    id: ClassVar[StrId] = StrId("agent")
    namespace: ClassVar[tuple[str, ...]] = ("agent",)

    schema: ClassVar[ObjectSchema[AgentConfig]] = ObjectSchema(
        description="Лимиты агентского лупа.",
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
        ],
        factory=AgentConfig,
    )


# ──────────────────────────────────────────────────────────────────────
# Factory and Loader
# ──────────────────────────────────────────────────────────────────────


class ConfigFactory:
    """Реестр секций + сборщик ConfigBundle.

    Регистрация секций — императивная (register). Discovery
    extension-секций — через entry-point group
    CONFIG_SECTIONS_ENTRY_POINT (discover_extension_sections).
    Сборка бандла — build: каждая зарегистрированная секция строит
    свой DTO, результат складывается по id.

    Использование: ``ConfigFactory()`` без аргументов; ``register(...)``
    + ``discover_extension_sections()`` собирают набор секций;
    ``attach_sources([...])`` принимает источники в порядке приоритета
    (cli > env > toml). ``build()`` собирает полный набор
    (ConfigKey, FieldSpec) пар по зарегистрированным секциям, дёргает
    ``bind_schema`` на каждом источнике (даёт CLI-источнику построить
    argparse, env/TOML — провалидировать ключи на опечатки), строит
    резолвер и собирает DTO каждой секции.
    """

    def __init__(self) -> None:
        self._sources: list[ConfigSource] = []
        self._sections: dict[StrId, ConfigSection[Any]] = {}
        self._resolver: ChainedConfigResolver | None = None
        self._bundle: ConfigBundle | None = None

    def attach_sources(self, sources: Sequence[ConfigSource]) -> None:
        """Подключить источники в порядке приоритета (cli > env > toml).
        Повторный вызов заменяет предыдущий список (idempotent);
        сбрасывает кеш резолвера и бандла — новые источники получат
        bind_schema, секции пересоберутся с новых данных.
        """
        self._sources = list(sources)
        self._resolver = None
        self._bundle = None

    def _iter_schema_items(self) -> Iterator[tuple[ConfigKey, FieldSpec[Any]]]:
        """Полный набор (key, field) пар по всем зарегистрированным
        секциям. Используется при сборке резолвера для bind_schema.
        """
        for section in self._sections.values():
            for fld in section.schema.fields:
                yield ConfigKey(*section.namespace, fld.name), fld

    def resolver(self) -> ChainedConfigResolver:
        """Лениво собранный резолвер; даёт каждому источнику увидеть
        полный набор ожидаемых ключей через bind_schema до первого
        resolve. Идемпотентен: повторные вызовы возвращают тот же
        инстанс. attach_sources сбрасывает кеш.
        """
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
        """Operator-readable рецепты «как задать этот ключ» — по одному
        на источник, который умеет адресовать ключ. Источники возвращают
        пустую строку, если не умеют — те в выдачу не попадают.
        """
        out: list[str] = []
        for src in self.resolver().sources:
            d = src.describe(key)
            if d:
                out.append(d)
        return out

    def format_config_error(self, err: ConverterInputError) -> str:
        """Operator-friendly текст ошибки конфига.

        Для FieldMissingError со известным ConfigKey добавляет recipe
        со списком способов задать значение, собранным из describe()
        всех подключённых источников. Для прочих ошибок (OneOf, ParseInt,
        cross-field invariants) — оставляет исходный message: значение
        было предоставлено, проблема не в «где взять», а в «что
        задано неверно».
        """
        # err.key типизирован как FieldAddress (=object) на уровне
        # declaration.py — этот слой не знает про ConfigKey. Здесь, в
        # infra, мы знаем: ConfigSection.build кладёт туда именно
        # ConfigKey. isinstance-narrowing восстанавливает тип для
        # type-checker'а и одновременно даёт runtime-проверку.
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
        """Подхватывает секции расширений через entry-point group
        boba.config_sections.

        Контракт entry-point: target — класс-наследник
        ConfigSection. Битые/некорректные entry-point'ы логируются
        warning'ом и пропускаются, чтобы один сломанный плагин не валил
        старт всего приложения.
        """
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
        """Собрать (или вернуть закешированный) ConfigBundle.

        Идемпотентно: повторные вызовы возвращают тот же инстанс — это
        важно потому что bind_schema источников имеет побочные эффекты
        (CliSource на ``--help`` вызывает ``sys.exit(0)``), и пересборка
        дала бы их повторно. Кеш сбрасывается при ``attach_sources``.
        """
        if self._bundle is None:
            resolver = self.resolver()
            built: dict[StrId, object] = {
                sid: section.build(resolver)
                for sid, section in self._sections.items()
            }
            self._bundle = ConfigBundle(built)
        return self._bundle
