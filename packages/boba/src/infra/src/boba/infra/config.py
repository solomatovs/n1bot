"""Сборка типизированной конфигурации приложения из секций.

Структурно:

- :class:`~boba.domain.core.config.ConfigSection`-секции (LLM, agent,
  workspaces, …) объявляют свои поля декларативно (через
  :class:`~boba.domain.core.config.FieldSpec` поверх
  :class:`~boba.domain.core.config.ConfigKey`) и строят типизированный
  DTO. Тот же примитив используется секциями расширений.
- :class:`ConfigFactory` регистрирует встроенные секции и подхватывает
  секции расширений через entry-point group ``boba.config_sections``.
  :meth:`ConfigFactory.build` возвращает :class:`ConfigBundle`.
- :class:`ConfigBundle` — итог сборки: ``dict[StrId, T_DTO]`` +
  типизированный доступ через :meth:`ConfigBundle.section`. Удобные
  свойства :attr:`ConfigBundle.app` и :attr:`ConfigBundle.agent`
  композируют :class:`AppConfig` / :class:`AgentConfig` из встроенных
  секций.
- :class:`ConfigLoader` — тонкая ленивая обёртка с кэшем бандла.

Конкретные :class:`ConfigSource`-реализации (env, TOML, …) живут в
**отдельных пакетах**: :mod:`boba_config_env`, :mod:`boba_config_toml`
и т.п. Bootstrap приложения сам собирает свою цепочку источников и
передаёт готовый :class:`~boba.domain.core.config.ChainedConfigResolver`
в :func:`default_config_factory`.

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
    Required,
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
    "LLMTransportSection",
    "PromptsSection",
    "WorkspacesSection",
    "default_config_factory",
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

    def resolve(self, spec: FieldSpec[Any]) -> object | None:
        return self._defaults.get(spec.key)


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


class ConfigBundle:
    """Итог сборки: типизированный реестр секций по :class:`StrId`.

    :meth:`section` достаёт DTO нужной секции по её классу — type-checker
    видит конкретный T_DTO. :attr:`app` / :attr:`agent` — удобные свойства
    для встроенных агрегатов.

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

    @property
    def app(self) -> AppConfig:
        """Композиция встроенных секций в :class:`AppConfig`."""
        core = self.section(AppCoreSection)
        return AppConfig(
            workspaces=self.section(WorkspacesSection),
            ssl_verify=core.ssl_verify,
            log_level=core.log_level,
            log_file=core.log_file,
            llm=self.section(LLMTransportSection),
            prompts_dir=self.section(PromptsSection),
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

    SSL_VERIFY: FieldSpec[bool] = FieldSpec(
        key=ConfigKey("app", "ssl_verify"),
        converter=ChainConverter(Default(False), ParseBool()),
        description="Проверять ли TLS-сертификат у HTTPS-запросов из приложения.",
    )
    LOG_LEVEL: FieldSpec[str] = FieldSpec(
        key=ConfigKey("app", "log_level"),
        converter=ChainConverter(Default("INFO"), ParseString()),
        description="Уровень корневого логгера: DEBUG/INFO/WARNING/ERROR/CRITICAL.",
    )
    LOG_FILE: FieldSpec[str | None] = FieldSpec(
        key=ConfigKey("app", "log_file"),
        converter=Nullable(ParseString()),
        description="Путь к log-файлу. Если пусто — логи только в stderr.",
    )

    fields: ClassVar[Sequence[FieldSpec[Any]]] = (SSL_VERIFY, LOG_LEVEL, LOG_FILE)

    def build(self, resolver: ChainedConfigResolver) -> AppCoreConfig:
        return AppCoreConfig(
            ssl_verify=self.SSL_VERIFY.read(resolver),
            log_level=self.LOG_LEVEL.read(resolver),
            log_file=self.LOG_FILE.read(resolver),
        )


class WorkspacesSection(ConfigSection[WorkspaceLayout]):
    """Раскладка namespace'ов workspace'а относительно ``base_dir``."""

    id: ClassVar[StrId] = StrId("workspaces")

    BASE_DIR: FieldSpec[str] = FieldSpec(
        key=ConfigKey("workspaces", "base_dir"),
        converter=ChainConverter(Default("./workspaces"), ParseString()),
        description="Корневая директория всех workspace-namespace'ов.",
    )
    USER: FieldSpec[str] = FieldSpec(
        key=ConfigKey("workspaces", "user_subdir"),
        converter=ChainConverter(Default("user"), ParseString()),
        description="Имя поддиректории user-workspace'а внутри base_dir.",
    )
    SYSTEM: FieldSpec[str] = FieldSpec(
        key=ConfigKey("workspaces", "system_subdir"),
        converter=ChainConverter(Default("system"), ParseString()),
        description="Имя поддиректории system-workspace'а внутри base_dir.",
    )
    TMP: FieldSpec[str] = FieldSpec(
        key=ConfigKey("workspaces", "tmp_subdir"),
        converter=ChainConverter(Default("tmp"), ParseString()),
        description="Имя поддиректории tmp-workspace'а внутри base_dir.",
    )

    fields: ClassVar[Sequence[FieldSpec[Any]]] = (BASE_DIR, USER, SYSTEM, TMP)

    def build(self, resolver: ChainedConfigResolver) -> WorkspaceLayout:
        return WorkspaceLayout(
            base_dir=self.BASE_DIR.read(resolver),
            user_subdir=self.USER.read(resolver),
            system_subdir=self.SYSTEM.read(resolver),
            tmp_subdir=self.TMP.read(resolver),
        )


class LLMTransportSection(ConfigSection[LLMConfig]):
    """Транспорт LLM-клиента: ``base_url`` + ``api_key``."""

    id: ClassVar[StrId] = StrId("llm_transport")

    BASE_URL: FieldSpec[str] = FieldSpec(
        key=ConfigKey("llm", "base_url"),
        converter=ChainConverter(
            Default("http://localhost:11434/v1"), ParseString(),
        ),
        description="OpenAI-совместимый base URL LLM-сервера (LiteLLM/Ollama/...).",
    )
    API_KEY: FieldSpec[str] = FieldSpec(
        key=ConfigKey("llm", "api_key"),
        converter=ChainConverter(Default("ollama"), ParseString()),
        description="API-ключ LLM-сервера. Для локального Ollama — любой непустой.",
    )

    fields: ClassVar[Sequence[FieldSpec[Any]]] = (BASE_URL, API_KEY)

    def build(self, resolver: ChainedConfigResolver) -> LLMConfig:
        return LLMConfig(
            base_url=self.BASE_URL.read(resolver),
            api_key=self.API_KEY.read(resolver),
        )


class AgentSection(ConfigSection[AgentConfig]):
    """Лимиты агентского лупа."""

    id: ClassVar[StrId] = StrId("agent")

    MAX_ITERATIONS: FieldSpec[int] = FieldSpec(
        key=ConfigKey("agent", "max_iterations"),
        converter=ChainConverter(Default(20), ParseInt(), MinValue(1)),
        description="Жёсткий потолок числа итераций агента в одной сессии.",
    )
    MAX_CONSECUTIVE_TOOL_CALLS: FieldSpec[int] = FieldSpec(
        key=ConfigKey("agent", "max_consecutive_tool_calls"),
        converter=ChainConverter(Default(3), ParseInt(), MinValue(1)),
        description="Сколько раз подряд агент может звать tools без LLM-ответа.",
    )

    fields: ClassVar[Sequence[FieldSpec[Any]]] = (
        MAX_ITERATIONS,
        MAX_CONSECUTIVE_TOOL_CALLS,
    )

    def build(self, resolver: ChainedConfigResolver) -> AgentConfig:
        return AgentConfig(
            max_iterations=self.MAX_ITERATIONS.read(resolver),
            max_consecutive_tool_calls=self.MAX_CONSECUTIVE_TOOL_CALLS.read(resolver),
        )


class PromptsSection(ConfigSection[str]):
    """Путь к директории с системными prompt'ами.

    ``DIR`` — обязательное поле: оператор должен явно указать, откуда
    :class:`~boba.infra.prompt_loader.PromptLoader` берёт system-prompt
    блоки при старте.
    """

    id: ClassVar[StrId] = StrId("prompts")

    DIR: FieldSpec[str] = FieldSpec(
        key=ConfigKey("prompts", "dir"),
        converter=ChainConverter(Required(), ParseString()),
        description="Корневая директория .md/.txt-файлов с system-prompt'ами.",
    )

    fields: ClassVar[Sequence[FieldSpec[Any]]] = (DIR,)

    def build(self, resolver: ChainedConfigResolver) -> str:
        return self.DIR.read(resolver)


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
    из подключённых пакетов (например, :mod:`boba_config_env` +
    :mod:`boba_config_toml`).
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


def default_config_factory(
    resolver: ChainedConfigResolver,
) -> ConfigFactory:
    """Фабрика приложения: регистрирует встроенные секции и подхватывает
    секции расширений через entry-point group ``boba.config_sections``.

    ``resolver`` — обязательный параметр: инфра не знает про конкретные
    источники, цепочку собирает bootstrap.
    """
    factory = ConfigFactory(resolver)
    factory.register(AppCoreSection())
    factory.register(WorkspacesSection())
    factory.register(LLMTransportSection())
    factory.register(AgentSection())
    factory.register(PromptsSection())
    factory.discover_extension_sections()
    return factory
