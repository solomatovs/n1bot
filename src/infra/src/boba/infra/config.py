"""Загрузка конфигурации приложения из env / env-файлов / TOML.

Структурно:

- :class:`ConfigSource` (в :mod:`boba.domain.core.config`) — источник
  значений по :class:`FieldSpec`. Реализации:
  :class:`EnvSource` / :class:`EnvFileSource` / :class:`TomlSource` /
  :class:`TomlFileSource` / :class:`DefaultSource`.
- :class:`ConfigSectionBuilder` — одна секция конфига (workspaces,
  llm, agent, …). Читает поля через ``FieldSpec`` и складывает в
  :class:`ConfigState`.
- :class:`ConfigFactory` — fold-factory, применяющий секции в
  порядке ``priority`` и финализирующий в :class:`ConfigBundle`.
- :class:`ConfigLoader` — тонкая обёртка с lazy-кэшем бандла.

:class:`ConfigBundle` / :class:`ConfigLoader` — **агрегат приложения**:
:class:`AppConfig` + :class:`AgentConfig`. То, что шарится на весь
процесс и читается один раз на старте.

LLM-sampling-параметров здесь нет: единственный источник —
:class:`~boba.domain.agent.models.AgentRequest.sampling`, прокидываемый
caller'ом (UI/CLI) per-request. См.
:class:`~boba.domain.agent.turn.reducers.AgentRequestSamplingReducer`.
"""

from __future__ import annotations

import os
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar, cast

from boba.domain.agent.models import AgentConfig
from boba.domain.config import AppConfig, LLMConfig, WorkspaceLayout
from boba.domain.core.config import (
    BoolConverter,
    ChainedConfigResolver,
    ConfigSource,
    FieldSpec,
    IntConverter,
    StrConverter,
)
from boba.domain.core.patterns import FoldFactory, PrioritySource, StrId

T = TypeVar("T")

__all__ = [
    "AgentSection",
    "AppCoreSection",
    "ConfigBundle",
    "ConfigError",
    "ConfigFactory",
    "ConfigLoader",
    "ConfigSectionBuilder",
    "ConfigSlot",
    "ConfigSlotMissingError",
    "ConfigState",
    "DefaultSource",
    "EnvFileSource",
    "EnvSource",
    "ExtensionsSection",
    "LLMTransportSection",
    "TomlFileSource",
    "TomlSource",
    "WorkspacesSection",
    "default_config_factory",
    "default_resolver",
    "load_toml",
]


class ConfigLoader:
    """Ленивый фасад над :class:`ConfigFactory`: кэширует бандл после
    первой сборки.

    Возвращает агрегат приложения (:class:`AppConfig` +
    :class:`AgentConfig`). LLM-sampling сюда не входит — он приезжает
    per-request через :class:`~boba.domain.agent.models.AgentRequest`.
    """

    def __init__(self, factory: ConfigFactory | None = None) -> None:
        self._factory = factory or default_config_factory(default_resolver())
        self._bundle: ConfigBundle | None = None

    def _ensure(self) -> ConfigBundle:
        if self._bundle is None:
            self._bundle = self._factory.build()

        return self._bundle

    def load_app(self) -> AppConfig:
        return self._ensure().app

    def load_agent(self) -> AgentConfig:
        return self._ensure().agent


@dataclass(frozen=True)
class ConfigBundle:
    app: AppConfig
    agent: AgentConfig


@dataclass
class ConfigState:
    """Накапливаемое состояние. Все слоты стартуют с ``None`` и заполняются
    ``apply()``-ом своей :class:`ConfigSectionBuilder`-секции. Какие
    из них на стадии :meth:`ConfigFactory.finalize` обязательны, а какие
    нет — декларируется через :class:`ConfigSlot`-константы внутри
    фабрики, не через дефолты dataclass'а.
    """

    resolver: ChainedConfigResolver
    workspaces: WorkspaceLayout | None = None
    llm_transport: LLMConfig | None = None
    ssl_verify: bool | None = None
    log_level: str | None = None
    log_file: str | None = None
    agent: AgentConfig | None = None
    extensions_dir: str | None = None


class ConfigError(Exception):
    """Базовая ошибка конфиг-инфры — отделяет сбои ConfigFactory/Slot
    от ошибок-резолверов (:class:`ConverterInputError` и потомков).
    """


class ConfigSlotMissingError(ConfigError):
    """Слот :class:`ConfigState` пуст на стадии ``finalize``: значит,
    соответствующая :class:`ConfigSectionBuilder` не была
    зарегистрирована в :class:`ConfigFactory`. Это инвариант сборки
    фабрики, не пользовательский сбой.
    """

    def __init__(self, attr: str, section: str) -> None:
        super().__init__(
            f"ConfigState slot {attr!r} is None: "
            f"{section} must be registered in ConfigFactory"
        )
        self.attr = attr
        self.section = section


@dataclass(frozen=True)
class ConfigSlot(Generic[T]):
    """Декларативное описание обязательного слота :class:`ConfigState`.

    Симметричная пара к :class:`FieldSpec`: ``FieldSpec`` декларирует,
    как взять значение из source-цепочки на стадии ``apply``;
    ``ConfigSlot`` декларирует, как достать валидированное значение из
    :class:`ConfigState` на стадии ``finalize``. ``None`` означает, что
    обязательная секция не была зарегистрирована в фабрике —
    fail-fast через :class:`ConfigSlotMissingError`.

    Использование — class-level константы внутри ``Factory``-класса,
    по аналогии с ``FieldSpec`` внутри ``Section``-класса::

        class ConfigFactory(...):
            _WORKSPACES = ConfigSlot[WorkspaceLayout](
                "workspaces", "WorkspacesSection"
            )

            def finalize(self, state):
                return ConfigBundle(
                    app=AppConfig(workspaces=self._WORKSPACES.read(state), ...),
                    ...
                )
    """

    attr: str
    section: str

    def read(self, state: ConfigState) -> T:
        value = getattr(state, self.attr)
        if value is None:
            raise ConfigSlotMissingError(self.attr, self.section)
        return cast(T, value)


class ConfigSectionBuilder(PrioritySource[StrId, ConfigState]):
    """Базовый класс одной секции. ``priority`` формален — секции независимы.

    ``TOML_PATHS`` — карта env-ключей в пути ``(toml_section, toml_key)``
    для полей, которые могут приходить из TOML. Фабрика собирает
    объединённую карту по всем зарегистрированным секциям и передаёт в
    TOML-источники.
    """

    TOML_PATHS: ClassVar[Mapping[str, tuple[str, str]]] = {}

    def __init__(self, priority: int) -> None:
        self._priority = priority

    @abstractmethod
    def id(self) -> StrId: ...

    def priority(self) -> int:
        return self._priority

    def toml_mapping(self) -> Mapping[str, tuple[str, str]]:
        return self.TOML_PATHS

    @abstractmethod
    def apply(self, state: ConfigState) -> ConfigState: ...


class AppCoreSection(ConfigSectionBuilder):
    SSL_VERIFY = FieldSpec("SSL_VERIFY", BoolConverter(), False)
    LOG_LEVEL = FieldSpec("LOG_LEVEL", StrConverter(), "INFO")
    LOG_FILE = FieldSpec("LOG_FILE", StrConverter(), None)

    TOML_PATHS: ClassVar[Mapping[str, tuple[str, str]]] = {
        "SSL_VERIFY": ("app", "ssl_verify"),
        "LOG_LEVEL": ("app", "log_level"),
        "LOG_FILE": ("app", "log_file"),
    }

    def id(self) -> StrId:
        return StrId("app_core")

    def apply(self, state: ConfigState) -> ConfigState:
        state.ssl_verify = self.SSL_VERIFY.read(state.resolver)
        state.log_level = self.LOG_LEVEL.read(state.resolver)
        state.log_file = self.LOG_FILE.read_opt(state.resolver)
        return state


class WorkspacesSection(ConfigSectionBuilder):
    BASE_DIR = FieldSpec("WORKSPACE_BASE_DIR", StrConverter(), "./workspaces")
    USER = FieldSpec("WORKSPACE_USER_SUBDIR", StrConverter(), "user")
    SYSTEM = FieldSpec("WORKSPACE_SYSTEM_SUBDIR", StrConverter(), "system")
    TMP = FieldSpec("WORKSPACE_TMP_SUBDIR", StrConverter(), "tmp")

    TOML_PATHS: ClassVar[Mapping[str, tuple[str, str]]] = {
        "WORKSPACE_BASE_DIR": ("workspaces", "base_dir"),
        "WORKSPACE_USER_SUBDIR": ("workspaces", "user"),
        "WORKSPACE_SYSTEM_SUBDIR": ("workspaces", "system"),
        "WORKSPACE_TMP_SUBDIR": ("workspaces", "tmp"),
    }

    def id(self) -> StrId:
        return StrId("workspaces")

    def apply(self, state: ConfigState) -> ConfigState:
        state.workspaces = WorkspaceLayout(
            base_dir=self.BASE_DIR.read(state.resolver),
            user_subdir=self.USER.read(state.resolver),
            system_subdir=self.SYSTEM.read(state.resolver),
            tmp_subdir=self.TMP.read(state.resolver),
        )
        return state


class LLMTransportSection(ConfigSectionBuilder):
    BASE_URL = FieldSpec("LLM_BASE_URL", StrConverter(), "http://localhost:11434/v1")
    API_KEY = FieldSpec("LITELLM_API_KEY", StrConverter(), "ollama")

    TOML_PATHS: ClassVar[Mapping[str, tuple[str, str]]] = {
        "LLM_BASE_URL": ("llm", "base_url"),
        "LITELLM_API_KEY": ("llm", "api_key"),
    }

    def id(self) -> StrId:
        return StrId("llm_transport")

    def apply(self, state: ConfigState) -> ConfigState:
        state.llm_transport = LLMConfig(
            base_url=self.BASE_URL.read(state.resolver),
            api_key=self.API_KEY.read(state.resolver),
        )
        return state


class AgentSection(ConfigSectionBuilder):
    MAX_ITERATIONS = FieldSpec("AGENT_MAX_ITERATIONS", IntConverter(), 20)
    MAX_CONSECUTIVE_TOOL_CALLS = FieldSpec(
        "AGENT_MAX_CONSECUTIVE_TOOL_CALLS", IntConverter(), 3
    )

    TOML_PATHS: ClassVar[Mapping[str, tuple[str, str]]] = {
        "AGENT_MAX_ITERATIONS": ("agent", "max_iterations"),
        "AGENT_MAX_CONSECUTIVE_TOOL_CALLS": ("agent", "max_consecutive_tool_calls"),
    }

    def id(self) -> StrId:
        return StrId("agent")

    def apply(self, state: ConfigState) -> ConfigState:
        state.agent = AgentConfig(
            max_iterations=self.MAX_ITERATIONS.read(state.resolver),
            max_consecutive_tool_calls=(
                self.MAX_CONSECUTIVE_TOOL_CALLS.read(state.resolver)
            ),
        )
        return state


class ExtensionsSection(ConfigSectionBuilder):
    """Секция конфига расширений: путь к директории с .py/.md/.txt.

    Поле ``BOBA_EXTENSIONS_DIR`` обязательно — без него
    :meth:`FieldSpec.read` бросит :class:`ConverterInputError`, и
    приложение не стартует. Оператор обязан явно указать, откуда
    :class:`~boba.infra.extensions.ExtensionLoader` берёт tools и
    prompts при старте (env, TOML ``[extensions] dir`` или
    ``_FILE``-секрет).
    """

    DIR = FieldSpec[str]("BOBA_EXTENSIONS_DIR", StrConverter())

    TOML_PATHS: ClassVar[Mapping[str, tuple[str, str]]] = {
        "BOBA_EXTENSIONS_DIR": ("extensions", "dir"),
    }

    def id(self) -> StrId:
        return StrId("extensions")

    def apply(self, state: ConfigState) -> ConfigState:
        state.extensions_dir = self.DIR.read(state.resolver)
        return state


class ConfigFactory(FoldFactory[StrId, ConfigState, ConfigBundle]):
    """Фабрика конфиг-бандла. Обязательные слоты :class:`ConfigState`
    декларируются как class-level :class:`ConfigSlot`-константы (см.
    ниже) — на стадии :meth:`finalize` они зачитываются и валидируются
    в одно действие. Опциональные поля (например, ``log_file``) читаются
    из ``state`` напрямую без слота — их ``None`` валидное значение.
    """

    _SLOT_WORKSPACES = ConfigSlot[WorkspaceLayout]("workspaces", "WorkspacesSection")
    _SLOT_SSL_VERIFY = ConfigSlot[bool]("ssl_verify", "AppCoreSection (ssl_verify)")
    _SLOT_LOG_LEVEL = ConfigSlot[str]("log_level", "AppCoreSection (log_level)")
    _SLOT_LLM = ConfigSlot[LLMConfig]("llm_transport", "LLMTransportSection")
    _SLOT_EXTENSIONS_DIR = ConfigSlot[str]("extensions_dir", "ExtensionsSection")
    _SLOT_AGENT = ConfigSlot[AgentConfig]("agent", "AgentSection")

    def __init__(self, resolver: ChainedConfigResolver) -> None:
        super().__init__()
        self._resolver = resolver

    def initial(self) -> ConfigState:
        return ConfigState(resolver=self._resolver)

    def finalize(self, state: ConfigState) -> ConfigBundle:
        app = AppConfig(
            workspaces=self._SLOT_WORKSPACES.read(state),
            ssl_verify=self._SLOT_SSL_VERIFY.read(state),
            log_level=self._SLOT_LOG_LEVEL.read(state),
            log_file=state.log_file,
            llm=self._SLOT_LLM.read(state),
            extensions_dir=self._SLOT_EXTENSIONS_DIR.read(state),
        )
        return ConfigBundle(
            app=app,
            agent=self._SLOT_AGENT.read(state),
        )


def default_resolver(
    extra_toml_paths: Mapping[str, tuple[str, str]] | None = None,
    extra_sources: Sequence[ConfigSource] = (),
) -> ChainedConfigResolver:
    """Стандартная цепочка: EnvFile → Env → TomlFile → Toml [→ extras].

    ``extra_toml_paths`` расширяют built-in карту TOML-путей — добавляй
    сюда маппинги полей из модулей-потребителей (chainlit и т.п.).
    """

    builtin_toml_paths = {
        **AppCoreSection.TOML_PATHS,
        **WorkspacesSection.TOML_PATHS,
        **LLMTransportSection.TOML_PATHS,
        **AgentSection.TOML_PATHS,
        **ExtensionsSection.TOML_PATHS,
    }

    toml_data: dict[str, Any] = load_toml(os.environ.get("BOBA_CONFIG"))
    path_map: dict[str, tuple[str, str]] = {**builtin_toml_paths}
    if extra_toml_paths:
        path_map.update(extra_toml_paths)
    sources: list[ConfigSource] = [
        EnvFileSource(),
        EnvSource(),
        TomlFileSource(toml_data, path_map),
        TomlSource(toml_data, path_map),
        *extra_sources,
    ]
    return ChainedConfigResolver(sources)


def default_config_factory(
    resolver: ChainedConfigResolver,
) -> ConfigFactory:
    """Фабрика приложения: app + agent.

    LLM-sampling сюда не входит — он приезжает per-request через
    :class:`~boba.domain.agent.models.AgentRequest`.
    """
    factory = ConfigFactory(resolver)
    factory.register(AppCoreSection(priority=10))
    factory.register(WorkspacesSection(priority=20))
    factory.register(LLMTransportSection(priority=30))
    factory.register(AgentSection(priority=40))
    factory.register(ExtensionsSection(priority=50))
    return factory


class EnvSource(ConfigSource):
    def resolve(self, spec: FieldSpec[Any]) -> object | None:
        return os.environ.get(spec.key)


class EnvFileSource(ConfigSource):
    """``{KEY}_FILE`` → путь к файлу-секрету (Docker-style)."""

    def resolve(self, spec: FieldSpec[Any]) -> object | None:
        path = os.environ.get(f"{spec.key}_FILE")
        if not path:
            return None
        p = Path(path)
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8").strip()


class TomlSource(ConfigSource):
    """Значение из TOML по карте ``{FieldSpec.key: (section, toml_key)}``.

    Если ключа нет в карте — пропуск: поле не предназначено для TOML.
    """

    def __init__(
        self,
        data: Mapping[str, Any],
        path_map: Mapping[str, tuple[str, str]],
    ) -> None:
        self._data = data
        self._path_map = path_map

    def resolve(self, spec: FieldSpec[Any]) -> object | None:
        path = self._path_map.get(spec.key)
        if path is None:
            return None
        section, toml_key = path
        section_data = self._data.get(section)
        if not isinstance(section_data, Mapping):
            return None
        return section_data.get(toml_key)


class TomlFileSource(ConfigSource):
    """``[section] {toml_key}_file`` → путь к файлу-секрету в TOML."""

    def __init__(
        self,
        data: Mapping[str, Any],
        path_map: Mapping[str, tuple[str, str]],
    ) -> None:
        self._data = data
        self._path_map = path_map

    def resolve(self, spec: FieldSpec[Any]) -> object | None:
        path = self._path_map.get(spec.key)
        if path is None:
            return None

        section, toml_key = path
        section_data = self._data.get(section)
        if not isinstance(section_data, Mapping):
            return None

        file_path = section_data.get(f"{toml_key}_file")
        if not isinstance(file_path, str):
            return None

        p = Path(file_path)
        if not p.is_file():
            return None

        return p.read_text(encoding="utf-8").strip()


class DefaultSource(ConfigSource):
    """Статический fallback-словарь — для тестов и кастомных пресетов."""

    def __init__(self, defaults: Mapping[str, object]) -> None:
        self._defaults = defaults

    def resolve(self, spec: FieldSpec[Any]) -> object | None:
        return self._defaults.get(spec.key)


def load_toml(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    # Битый TOML не глотаем — это инвариант-нарушение, пусть падает громко.
    import tomli  # noqa: PLC0415

    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("rb") as f:
        return tomli.load(f)
