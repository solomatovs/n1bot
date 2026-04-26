"""Сборка типизированной конфигурации приложения из секций.

Структурно:

- :class:`~boba.domain.core.config.ConfigSection`-секции (agent, app_core,
  плюс секции адаптеров: ``LLMTransportSection`` /
  ``WorkspacesSection`` / ``PromptsSection`` живут в соответствующих
  ``boba-adapter-*`` пакетах) объявляют свои поля декларативно (через
  :class:`~boba.domain.core.config.FieldSpec` поверх
  :class:`~boba.domain.core.config.ConfigKey`) и строят типизированный
  DTO.
- :class:`ConfigFactory` регистрирует секции и подхватывает секции
  расширений через entry-point group ``boba.config_sections``.
  :meth:`ConfigFactory.build` возвращает :class:`ConfigBundle`.
- :class:`ConfigBundle` — итог сборки: ``dict[StrId, T_DTO]`` +
  типизированный доступ через :meth:`ConfigBundle.section`. Удобные
  свойства :attr:`ConfigBundle.app` и :attr:`ConfigBundle.agent`
  композируют :class:`AppConfig` / :class:`AgentConfig` из секций
  ``app_core``, ``workspaces``, ``llm_transport``, ``prompts``,
  ``agent`` (последние три приходят из адаптерных пакетов).
- :class:`ConfigLoader` — тонкая ленивая обёртка с кэшем бандла.

Bootstrap приложения собирает цепочку :class:`ConfigSource`-источников
(env/TOML/…), создаёт :class:`ConfigFactory`, регистрирует
встроенные :class:`AppCoreSection`/:class:`AgentSection` и нужные
adapter-секции, и затем зовёт :meth:`build`.

LLM-sampling-параметров здесь нет: единственный источник —
:class:`~boba.domain.agent.models.AgentRequest.sampling`, прокидываемый
caller'ом (UI/CLI) per-request.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar, cast

from boba.domain.agent.models import AgentConfig
from boba.domain.config import AppConfig, LLMConfig, WorkspaceLayout
from boba.domain.core.config import (
    ChainedConfigResolver,
    ConfigKey,
    ConfigSection,
    ConfigSource,
    FieldSpec,
    ObjectSchema,
)
from boba.domain.core.patterns import StrId
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
    "ConfigLoader",
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

    Принимает :class:`ConfigKey` → значение. Возвращает значение по точному
    совпадению ключа; неизвестные ключи — ``None``. Обычно ставят последним
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
    ошибок-резолверов (:class:`ConverterInputError` и потомков).
    """


class ConfigSectionAlreadyRegisteredError(ConfigError):
    """Попытка зарегистрировать вторую секцию с тем же :class:`StrId`."""

    def __init__(self, section_id: StrId) -> None:
        super().__init__(f"ConfigSection {section_id!r} is already registered")
        self.section_id = section_id


class ConfigSectionMissingError(ConfigError):
    """Запрошен ``bundle.section(SectionCls)``, но секция не зарегистрирована.

    Это инвариант сборки фабрики: секция должна быть зарегистрирована до
    :meth:`ConfigFactory.build`. Для встроенных секций гарантируется
    :func:`default_config_factory`; для секций расширений — discovery через
    entry-point group ``boba.config_sections``.
    """

    def __init__(self, section_cls: type[ConfigSection[Any]]) -> None:
        super().__init__(
            f"ConfigSection {section_cls.__name__!r} (id={section_cls.id!r}) "
            "is not registered in factory"
        )
        self.section_cls = section_cls


@dataclass(frozen=True)
class AppCoreConfig:
    """Внутренний DTO :class:`AppCoreSection`.

    Не часть публичного API — :class:`AppConfig` агрегирует поля плоско,
    эта структура нужна только чтобы :class:`AppCoreSection.build` имела
    типизированный return-type, симметричный остальным секциям.
    """

    ssl_verify: bool
    log_level: str
    log_file: str | None


_WORKSPACES_ID = StrId("workspaces")
_LLM_TRANSPORT_ID = StrId("llm_transport")
_PROMPTS_ID = StrId("prompts")


class ConfigBundle:
    """Итог сборки: типизированный реестр секций по :class:`StrId`.

    :meth:`section` достаёт DTO нужной секции по её классу — type-checker
    видит конкретный T_DTO. :attr:`app` / :attr:`agent` — удобные свойства
    для типичного app-стека: первый собирает :class:`AppConfig` из
    ``app_core`` + adapter-секций (``workspaces``/``llm_transport``/
    ``prompts``), второй — DTO ``agent``.

    Бандл иммутабельный (внутренний dict копируется при создании).
    """

    def __init__(self, sections: Mapping[StrId, object]) -> None:
        self._sections: dict[StrId, object] = dict(sections)

    def section(self, cls: type[ConfigSection[T]]) -> T:
        """Достать DTO секции ``cls``. Бросает
        :class:`ConfigSectionMissingError`, если секция не была
        зарегистрирована в фабрике.
        """
        sid = cls.id
        if sid not in self._sections:
            raise ConfigSectionMissingError(cls)
        return cast(T, self._sections[sid])

    def _by_id(self, section_id: StrId) -> object:
        """Сырой лукап по id — используется агрегатами :attr:`app` /
        :attr:`agent`, чтобы не зависеть от классов секций (они живут в
        adapter-пакетах). Если секции с таким id нет — :class:`ConfigError`.
        """
        if section_id not in self._sections:
            raise ConfigError(
                f"ConfigSection with id {section_id!r} is not registered "
                "in factory; required by ConfigBundle.app/agent aggregate"
            )
        return self._sections[section_id]

    @property
    def app(self) -> AppConfig:
        """Композиция ``app_core`` + ``workspaces`` + ``llm_transport`` +
        ``prompts`` в :class:`AppConfig`. Adapter-секции должны быть
        зарегистрированы в фабрике.
        """
        core = self.section(AppCoreSection)
        return AppConfig(
            workspaces=cast(WorkspaceLayout, self._by_id(_WORKSPACES_ID)),
            ssl_verify=core.ssl_verify,
            log_level=core.log_level,
            log_file=core.log_file,
            llm=cast(LLMConfig, self._by_id(_LLM_TRANSPORT_ID)),
            prompts_dir=cast(str, self._by_id(_PROMPTS_ID)),
        )

    @property
    def agent(self) -> AgentConfig:
        return self.section(AgentSection)


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
    """Реестр секций + сборщик :class:`ConfigBundle`.

    Регистрация секций — императивная (:meth:`register`). Discovery
    extension-секций — через entry-point group
    :data:`CONFIG_SECTIONS_ENTRY_POINT` (:meth:`discover_extension_sections`).
    Сборка бандла — :meth:`build`: каждая зарегистрированная секция строит
    свой DTO, результат складывается по :attr:`ConfigSection.id`.
    """

    def __init__(self, resolver: ChainedConfigResolver) -> None:
        self._resolver = resolver
        self._sections: dict[StrId, ConfigSection[Any]] = {}

    def register(self, section: ConfigSection[Any]) -> None:
        sid = section.id
        if sid in self._sections:
            raise ConfigSectionAlreadyRegisteredError(sid)
        self._sections[sid] = section

    def registered(self) -> Sequence[ConfigSection[Any]]:
        return tuple(self._sections.values())

    def discover_extension_sections(self) -> None:
        """Подхватывает секции расширений через entry-point group
        ``boba.config_sections``.

        Контракт entry-point: target — класс-наследник
        :class:`ConfigSection`. Битые/некорректные entry-point'ы логируются
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
        built: dict[StrId, object] = {}
        for sid, section in self._sections.items():
            built[sid] = section.build(self._resolver)
        return ConfigBundle(built)


class ConfigLoader:
    """Ленивый фасад над :class:`ConfigFactory`: кэширует бандл после
    первой сборки.

    Конструктор принимает уже собранную фабрику — у инфры нет своих
    источников значений, цепочку резолвера собирает bootstrap приложения
    из подключённых пакетов (например, :mod:`boba.config.env` +
    :mod:`boba.config.toml`).
    """

    def __init__(self, factory: ConfigFactory) -> None:
        self._factory = factory
        self._bundle: ConfigBundle | None = None

    def _ensure(self) -> ConfigBundle:
        if self._bundle is None:
            self._bundle = self._factory.build()
        return self._bundle

    def load_bundle(self) -> ConfigBundle:
        return self._ensure()

    def load_app(self) -> AppConfig:
        return self._ensure().app

    def load_agent(self) -> AgentConfig:
        return self._ensure().agent
