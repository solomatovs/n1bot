"""AgentBuilder — fluent-фасад для сборки Agent с разумными дефолтами."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from boba.agent.agent import Agent, AgentContext
from boba.agent.events import AgentEvent
from boba.agent.history import HistoryService, HistoryWriter, InMemoryHistoryService
from boba.agent.history_view import HistoryDialogView
from boba.agent.middleware import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    AssistantSnapshotMiddleware,
    EventStamperMiddleware,
    HistoryRecorderMiddleware,
    IterationCounterConfig,
    IterationCounterMiddleware,
    LLMPort,
    StopOnAnyFailure,
    StopOnFinished,
    ToolExecutionMiddleware,
    UserQueryRecorderMiddleware,
)
from boba.agent.turn.builder import TurnBuilder
from boba.llm.builder import LLM
from boba.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
    StreamSourceLoop,
)
from boba.plugin import ExtensionContext, Plugin, is_enabled, resolve_config_type
from boba.plugin.discovery import DEFAULT_PLUGIN_ENTRY_POINT_GROUP, discover_plugins
from boba.tools.domain import ToolSourceId
from boba.tools.framework import (
    StaticToolSource,
    ToolDecoratorFactory,
    ToolExecutor,
    ToolRegistry,
    ToolSource,
)

EventStamperFactory = Callable[
    [StreamSource[AgentContext, AgentEvent]],
    StreamSource[AgentContext, AgentEvent],
]


class AgentBuilderConfig(BaseModel):
    """DTO bootstrap-конфига AgentBuilder.

    Агрегирует per-middleware конфиги, нужные на этапе сборки агента.
    Дефолтный экземпляр создаётся в `AgentBuilder.__init__`; перезаписать
    можно через `AgentBuilder.use_config(...)`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    iteration_counter: IterationCounterConfig = Field(
        default_factory=IterationCounterConfig,
    )


class AgentBuilder:
    """Fluent-фасад: собирает Agent с дефолтной middleware-цепью.

    Owns: LLM, history journal, tool registry (sources +
    lifecycle), bootstrap-конфиг middleware-цепочки, event-stamper.
    Turn-side (model/prompts/sampling/reducers) живёт в `TurnBuilder` и
    подаётся одним методом `.use_turn(turn_builder)`.
    """

    def __init__(self) -> None:
        self._llm: LLM | None = None
        self._tool_registry: ToolRegistry | None = None
        self._inline_factories: list[ToolDecoratorFactory] = []
        self._plugin_entries: list[
            tuple[type[Plugin[Any, ToolSource]], Any | None]
        ] = []
        self._discover_groups: list[str] = []
        self._extensions: dict[type, object] = {}
        self._resolved_tool_registry: ToolRegistry | None = None
        self._history_service: HistoryService = InMemoryHistoryService()
        self._turn: TurnBuilder | None = None
        self._event_stamper_factory: EventStamperFactory = EventStamperMiddleware
        # Bootstrap-конфиг middleware-цепочки; переопределяется .use_config().
        self._builder_config: AgentBuilderConfig = AgentBuilderConfig()

    def with_llm(self, llm: LLM) -> Self:
        """Готовый LLM (обязательно; см. LLMBuilder)."""
        self._llm = llm
        return self

    def with_tools(self, registry: ToolRegistry) -> Self:
        """Готовый `ToolRegistry`; mutually-exclusive с use_*-путями."""
        self._tool_registry = registry
        return self

    def use_tools(self, factories: Iterable[ToolDecoratorFactory]) -> Self:
        """Добавить `@tool`-функции под общим source_id"""
        self._inline_factories.extend(factories)
        return self

    def with_extension(self, key: type, instance: object) -> Self:
        """
        Зарегистрировать build-time extension для `Plugin.build(ctx)`

        Plugin внутри `build` запрашивает зависимость по типу:
        `ctx.get(ProjectWorkspaceRegistry)`.

        Повторная регистрация того же ключа — `ValueError`
        """
        if key in self._extensions:
            msg = (
                f"AgentBuilder.with_extension: extension {key.__name__!r} "
                f"уже зарегистрирован"
            )
            raise ValueError(msg)
        if not isinstance(instance, key):
            msg = (
                f"AgentBuilder.with_extension: instance типа "
                f"{type(instance).__name__!r} не является {key.__name__!r}"
            )
            raise TypeError(msg)
        self._extensions[key] = instance
        return self

    def use_tools_plugins_discovered(
        self,
        group: str = DEFAULT_PLUGIN_ENTRY_POINT_GROUP,
    ) -> Self:
        """Подцепить все entry-point плагины group; фильтр `tool.<NAME>.enable`."""
        self._discover_groups.append(group)
        return self

    def tool_registry(self) -> ToolRegistry:
        """Собрать (и закешировать) `ToolRegistry` без сборки Agent."""
        return self._resolve_tool_registry()

    def use_tools_plugin(
        self,
        plugin: type[Plugin[Any, ToolSource]] | str,
        *,
        config: Any | None = None,
    ) -> Self:
        """Добавить tool-плагин.

        `plugin` — класс `Plugin` или строка `module:attr`.
        `config`:
        - готовый DTO — пробрасывается as-is;
        - `None` — материализуется через `cfg_type.load()` (env + TOML).
        """
        self._plugin_entries.append((self._resolve_plugin(plugin), config))
        return self

    def pipe(
        self,
        fn: Callable[..., Self],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Self:
        """Extension-style: `fn(self, *args, **kwargs) -> Self`."""
        return fn(self, *args, **kwargs)

    def with_history(self, service: HistoryService) -> Self:
        """Журнал AgentEvent; дефолт — InMemoryHistoryService()."""
        self._history_service = service
        return self

    def use_turn(self, turn: TurnBuilder) -> Self:
        """Описание следующего хода. Обязательно до `.build()`.

        Auto-wiring: если `turn` не задал `history_view`/`tool_catalog` явно,
        AgentBuilder при `.build()` подкладывает `HistoryDialogView` поверх
        собственного `HistoryService` и `ToolRegistry.catalog()`. Явно
        заданное в `turn` уважается — удобная override-точка для тестов.
        """
        self._turn = turn
        return self

    def use_config(self, config: AgentBuilderConfig) -> Self:
        """Заменить bootstrap-конфиг builder'а целиком.

        Используется, когда конфиг приходит из внешней системы
        (env / TOML / DI-контейнер). По умолчанию builder создаёт
        `AgentBuilderConfig()` со всеми дефолтами.
        """
        self._builder_config = config
        return self

    def use_event_stamper(self, factory: EventStamperFactory) -> Self:
        """Переопределить EventStamper-middleware своей фабрикой.

        Фабрика принимает inner-стрим и возвращает обёртку — passthrough,
        который проставляет envelope-поля (seq / emitted_at / iteration)
        на каждом проходящем AgentEvent. Дефолт — `EventStamperMiddleware`.
        """
        self._event_stamper_factory = factory
        return self

    def build(self) -> Agent:
        """Собрать Agent. Требуются `.with_llm(...)` и `.use_turn(...)`."""
        if self._llm is None:
            msg = "AgentBuilder.build: .with_llm(...) обязателен до .build()"
            raise ValueError(msg)
        if self._turn is None:
            msg = "AgentBuilder.build: .use_turn(...) обязателен до .build()"
            raise ValueError(msg)

        registry = self._resolve_tool_registry()
        history_service = self._history_service

        if not self._turn.has_history_view():
            self._turn.with_history_view(HistoryDialogView(history_service))
        if not self._turn.has_tool_catalog():
            self._turn.with_tool_catalog(registry.catalog())

        chain = self._build_chain(
            llm=self._llm,
            history_writer=history_service,
            tool_executor=registry.executor(),
            turn=self._turn,
            event_stamper_factory=self._event_stamper_factory,
            builder_config=self._builder_config,
        )
        source = StreamSourceLoop(
            source=chain,
            stop_if=StopOnFinished().or_(StopOnAnyFailure()),
        )
        return Agent(source=source)

    def _resolve_tool_registry(self) -> ToolRegistry:
        """Выбрать готовый `ToolRegistry` или собрать из накопленных источников."""
        has_accumulated = (
            bool(self._inline_factories)
            or bool(self._plugin_entries)
            or bool(self._discover_groups)
        )
        if self._tool_registry is not None and has_accumulated:
            msg = (
                "AgentBuilder.build: .with_tools(...) взаимоисключающий "
                "с use_*-путями — задан один маршрут"
            )
            raise ValueError(msg)
        if self._tool_registry is not None:
            return self._tool_registry

        if self._resolved_tool_registry is not None:
            return self._resolved_tool_registry

        if not has_accumulated:
            msg = (
                "AgentBuilder.build: задайте инструменты через "
                ".with_tools(...), .use_tools(...), .use_tools_plugin(...) "
                "или .use_tools_plugins_discovered(...)"
            )
            raise ValueError(msg)

        discovered: list[tuple[type[Plugin[Any, ToolSource]], None]] = []
        for group in self._discover_groups:
            for cls in discover_plugins(group):
                discovered.append((cls, None))

        sources: list[ToolSource] = []
        if self._inline_factories:
            sid = ToolSourceId("inline")
            sources.append(
                StaticToolSource(
                    sid,
                    [f.build(sid) for f in self._inline_factories],
                ),
            )
        ctx = ExtensionContext(self._extensions)
        for plugin_cls, config in (*self._plugin_entries, *discovered):
            cfg = self._materialize_plugin_config(plugin_cls, config)
            if not is_enabled(cfg):
                continue
            sources.extend(plugin_cls.build(cfg, ctx))
        self._resolved_tool_registry = ToolRegistry.from_sources(sources)
        return self._resolved_tool_registry

    @staticmethod
    def _resolve_plugin(
        plugin: type[Plugin[Any, ToolSource]] | str,
    ) -> type[Plugin[Any, ToolSource]]:
        """Идентификатор → класс плагина: либо сам класс, либо `module:attr`."""
        if isinstance(plugin, type):
            return plugin
        if ":" not in plugin:
            msg = (
                f"AgentBuilder.use_tools_plugin: ожидается класс Plugin или "
                f"строка вида 'module:attr', получено: {plugin!r}"
            )
            raise ValueError(msg)
        module_name, attr = plugin.split(":", 1)
        module = importlib.import_module(module_name)
        return getattr(module, attr)

    @staticmethod
    def _materialize_plugin_config(
        plugin_cls: type[Plugin[Any, ToolSource]],
        config: Any | None,
    ) -> Any:
        """`config` → DTO; None → `cfg_type.load()` (env + TOML)."""
        if config is not None:
            return config
        cfg_type = resolve_config_type(plugin_cls)
        if hasattr(cfg_type, "load"):
            return cfg_type.load()
        return cfg_type()  # type: ignore[call-arg]

    @staticmethod
    def _build_chain(
        *,
        llm: LLM,
        history_writer: HistoryWriter,
        tool_executor: ToolExecutor,
        turn: TurnBuilder,
        event_stamper_factory: EventStamperFactory,
        builder_config: AgentBuilderConfig,
    ) -> StreamSource[AgentContext, AgentEvent]:
        error_router = AgentErrorRouter()
        builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
        # HistoryRecorder самым внешним — журналит уже стампленные события.
        builder.use(
            lambda inner: HistoryRecorderMiddleware(inner, history_writer),
        )
        # EventStamper — сразу под HistoryRecorder: стампит ВСЁ, что приходит
        # от внутренних middleware, до записи в журнал и отдачи в sink'и.
        builder.use(event_stamper_factory)
        builder.use(lambda inner: AgentErrorRouterMiddleware(inner, error_router))
        builder.use(
            lambda inner: IterationCounterMiddleware(
                inner,
                builder_config.iteration_counter.max_iterations,
            ),
        )
        builder.use(
            lambda inner: ToolExecutionMiddleware(inner, tool_executor),
        )
        builder.use(AssistantSnapshotMiddleware)
        # UserQueryRecorder — самый внутренний wrapper над LLMPort: эмитит
        # UserQueryReceived один раз на request_id раньше любых LLM-событий.
        builder.use(UserQueryRecorderMiddleware)
        return builder.terminal(LLMPort(llm, turn))
