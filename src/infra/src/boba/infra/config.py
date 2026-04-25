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

Разделение ответственности:

- :class:`ConfigBundle` / :class:`ConfigLoader` — **агрегат приложения**:
  :class:`AppConfig` + :class:`AgentConfig`. То, что шарится на весь
  процесс и читается один раз на старте.
- :class:`SamplingParams` — параметры :class:`~boba.domain.agent.turn.\
reducers.SamplingReducer` и не часть bundle. Загружаются отдельно
  через :class:`SamplingLoader`, чтобы LLM-специфика не протекала в
  общий agent/app-агрегат.

Оба загрузчика переиспользуют одну и ту же инфраструктуру источников
(:func:`default_resolver`), так что env/TOML-картина для sampling
совместима с общим конфигом.
"""

from __future__ import annotations

import os
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from boba.domain.agent.models import AgentConfig
from boba.domain.config import AppConfig, LLMConfig, WorkspaceLayout
from boba.domain.core.config import (
    BoolConverter,
    ChainedConfigResolver,
    ConfigSource,
    CsvListConverter,
    FieldSpec,
    FloatConverter,
    IntConverter,
    StrConverter,
)
from boba.domain.core.patterns import FoldFactory, PrioritySource, StrId
from boba.domain.llm.models import SamplingParams

__all__ = [
    "AgentSection",
    "AppCoreSection",
    "ConfigBundle",
    "ConfigFactory",
    "ConfigLoader",
    "ConfigSectionBuilder",
    "ConfigState",
    "DefaultSource",
    "EnvFileSource",
    "EnvSource",
    "LLMSamplingSection",
    "LLMTransportSection",
    "SamplingLoader",
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
    :class:`AgentConfig`). :class:`SamplingParams` здесь сознательно
    нет — см. :class:`SamplingLoader`.
    """

    def __init__(self, factory: ConfigFactory | None = None) -> None:
        self._factory = factory or default_config_factory()
        self._bundle: ConfigBundle | None = None

    def _ensure(self) -> ConfigBundle:
        if self._bundle is None:
            self._bundle = self._factory.build()
        return self._bundle

    def load_app(self) -> AppConfig:
        return self._ensure().app

    def load_agent(self) -> AgentConfig:
        return self._ensure().agent


class SamplingLoader:
    """Отдельный загрузчик :class:`SamplingParams` для
    :class:`~boba.domain.agent.turn.reducers.SamplingReducer`.

    Почему отдельно: sampling — это параметры конкретного reducer'а
    TurnSpec, а не уровня приложения. Держим его вне
    :class:`ConfigBundle`, чтобы LLM-специфика не протекала в общий
    agent/app-агрегат. Переиспользует :func:`default_resolver` для
    единой картины env/TOML-источников с :class:`ConfigLoader`.
    """

    def __init__(
        self,
        resolver: ChainedConfigResolver | None = None,
    ) -> None:
        self._section = LLMSamplingSection(priority=0)
        self._state = ConfigState(resolver=resolver or default_resolver())
        self._cached: SamplingParams | None = None

    def load(self) -> SamplingParams:
        if self._cached is None:
            self._section.apply(self._state)
            self._cached = self._state.sampling or SamplingParams()
        return self._cached


@dataclass(frozen=True)
class ConfigBundle:
    app: AppConfig
    agent: AgentConfig


@dataclass
class ConfigState:
    """Накапливаемое состояние. ``None``-слот → дефолт dataclass'а в finalize.

    ``sampling`` намеренно присутствует: :class:`LLMSamplingSection`
    складывает сюда значение, но финализатор :class:`ConfigFactory`
    его игнорирует (sampling не часть :class:`ConfigBundle`).
    Поле нужно :class:`SamplingLoader`, который вызывает
    :meth:`LLMSamplingSection.apply` напрямую.
    """

    resolver: ChainedConfigResolver
    workspaces: WorkspaceLayout | None = None
    llm_transport: LLMConfig | None = None
    ssl_verify: bool | None = None
    log_level: str | None = None
    log_file: str | None = None
    agent: AgentConfig | None = None
    sampling: SamplingParams | None = None


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
    MAX_CONSECUTIVE_FORMAT_FAILURES = FieldSpec(
        "AGENT_MAX_CONSECUTIVE_FORMAT_FAILURES", IntConverter(), 3
    )

    TOML_PATHS: ClassVar[Mapping[str, tuple[str, str]]] = {
        "AGENT_MAX_ITERATIONS": ("agent", "max_iterations"),
        "AGENT_MAX_CONSECUTIVE_TOOL_CALLS": ("agent", "max_consecutive_tool_calls"),
        "AGENT_MAX_CONSECUTIVE_FORMAT_FAILURES": (
            "agent",
            "max_consecutive_format_failures",
        ),
    }

    def id(self) -> StrId:
        return StrId("agent")

    def apply(self, state: ConfigState) -> ConfigState:
        state.agent = AgentConfig(
            max_iterations=self.MAX_ITERATIONS.read(state.resolver),
            max_consecutive_tool_calls=(
                self.MAX_CONSECUTIVE_TOOL_CALLS.read(state.resolver)
            ),
            max_consecutive_format_failures=(
                self.MAX_CONSECUTIVE_FORMAT_FAILURES.read(state.resolver)
            ),
        )
        return state


class LLMSamplingSection(ConfigSectionBuilder):
    """Секция TOML ``[llm]`` → :class:`SamplingParams` (без обёрток)."""

    TEMPERATURE = FieldSpec("LLM_TEMPERATURE", FloatConverter(), None)
    TOP_P = FieldSpec("LLM_TOP_P", FloatConverter(), None)
    MAX_TOKENS = FieldSpec("LLM_MAX_TOKENS", IntConverter(), None)
    SEED = FieldSpec("LLM_SEED", IntConverter(), None)
    STOP = FieldSpec("LLM_STOP", CsvListConverter(), None)
    FREQUENCY_PENALTY = FieldSpec("LLM_FREQUENCY_PENALTY", FloatConverter(), None)
    PRESENCE_PENALTY = FieldSpec("LLM_PRESENCE_PENALTY", FloatConverter(), None)

    TOML_PATHS: ClassVar[Mapping[str, tuple[str, str]]] = {
        "LLM_TEMPERATURE": ("llm", "temperature"),
        "LLM_TOP_P": ("llm", "top_p"),
        "LLM_MAX_TOKENS": ("llm", "max_tokens"),
        "LLM_SEED": ("llm", "seed"),
        "LLM_STOP": ("llm", "stop"),
        "LLM_FREQUENCY_PENALTY": ("llm", "frequency_penalty"),
        "LLM_PRESENCE_PENALTY": ("llm", "presence_penalty"),
    }

    def id(self) -> StrId:
        return StrId("llm_sampling")

    def apply(self, state: ConfigState) -> ConfigState:
        r = state.resolver
        stop = self.STOP.read_opt(r)
        state.sampling = SamplingParams(
            temperature=self.TEMPERATURE.read_opt(r),
            top_p=self.TOP_P.read_opt(r),
            max_tokens=self.MAX_TOKENS.read_opt(r),
            seed=self.SEED.read_opt(r),
            stop=tuple(stop) if stop is not None else None,
            frequency_penalty=self.FREQUENCY_PENALTY.read_opt(r),
            presence_penalty=self.PRESENCE_PENALTY.read_opt(r),
        )
        return state


class ConfigFactory(FoldFactory[StrId, ConfigState, ConfigBundle]):
    def __init__(self, resolver: ChainedConfigResolver) -> None:
        super().__init__()
        self._resolver = resolver

    def initial(self) -> ConfigState:
        return ConfigState(resolver=self._resolver)

    def finalize(self, state: ConfigState) -> ConfigBundle:
        app = AppConfig(
            workspaces=state.workspaces or WorkspaceLayout(),
            ssl_verify=state.ssl_verify if state.ssl_verify is not None else False,
            log_level=state.log_level or "INFO",
            log_file=state.log_file,
            llm=state.llm_transport or LLMConfig(),
        )
        return ConfigBundle(
            app=app,
            agent=state.agent or AgentConfig(),
        )


_BUILTIN_TOML_PATHS: dict[str, tuple[str, str]] = {
    **AppCoreSection.TOML_PATHS,
    **WorkspacesSection.TOML_PATHS,
    **LLMTransportSection.TOML_PATHS,
    **AgentSection.TOML_PATHS,
    # LLMSamplingSection.TOML_PATHS добавляются автоматически в
    # default_resolver ниже — чтобы sampling читался из того же
    # TOML-файла без явной регистрации секции в ConfigFactory.
    **LLMSamplingSection.TOML_PATHS,
}


def default_resolver(
    extra_toml_paths: Mapping[str, tuple[str, str]] | None = None,
    extra_sources: Sequence[ConfigSource] = (),
) -> ChainedConfigResolver:
    """Стандартная цепочка: EnvFile → Env → TomlFile → Toml [→ extras].

    ``extra_toml_paths`` расширяют built-in карту TOML-путей — добавляй
    сюда маппинги полей из модулей-потребителей (chainlit и т.п.).
    """
    toml_data: dict[str, Any] = load_toml(os.environ.get("BOBA_CONFIG"))
    path_map: dict[str, tuple[str, str]] = {**_BUILTIN_TOML_PATHS}
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
    resolver: ChainedConfigResolver | None = None,
) -> ConfigFactory:
    """Фабрика приложения: app + agent. Sampling — в :class:`SamplingLoader`."""
    factory = ConfigFactory(resolver or default_resolver())
    factory.register(AppCoreSection(priority=10))
    factory.register(WorkspacesSection(priority=20))
    factory.register(LLMTransportSection(priority=30))
    factory.register(AgentSection(priority=40))
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
