"""AgentBuilder — fluent-фасад для сборки Agent с разумными дефолтами."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import Any, Self

from boba.agent.events import AgentEvent
from boba.agent.history import HistoryService, HistoryWriter, InMemoryHistoryService
from boba.agent.messages import InMemoryMessageService, MessageService, MessageWriter
from boba.agent.middleware import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    AssistantMessagePersistenceMiddleware,
    EventStamperMiddleware,
    HistoryRecorderMiddleware,
    IterationCounterMiddleware,
    LLMInvokeMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
    ToolExecutionMiddleware,
)
from boba.agent.orchestrator import Agent, AgentConfig, AgentContext
from boba.agent.prompt import PromptProvider
from boba.agent.turn.builder import TurnReducerFactory, TurnSpecBuilder
from boba.agent.turn.reducers import (
    AgentRequestSamplingReducer,
    HistoryReducer,
    ModelReducer,
    SystemPromptReducer,
    ToolsReducer,
    TurnReducer,
)
from boba.llm.builder import LLMPipeline
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
    ToolSource,
)

EventStamperFactory = Callable[
    [StreamSource[AgentContext, AgentEvent]],
    StreamSource[AgentContext, AgentEvent],
]


class AgentBuilder:
    """Fluent-фасад: собирает Agent с дефолтной middleware-цепью."""

    def __init__(self) -> None:
        self._llm: LLMPipeline | None = None
        self._tool_executor: ToolExecutor | None = None
        self._inline_factories: list[ToolDecoratorFactory] = []
        self._plugin_entries: list[
            tuple[type[Plugin[Any, ToolSource]], Any | None]
        ] = []
        self._discover_groups: list[str] = []
        self._extensions: dict[type, object] = {}
        self._resolved_tool_executor: ToolExecutor | None = None
        self._message_service: MessageService = InMemoryMessageService()
        self._history_service: HistoryService = InMemoryHistoryService()
        self._prompt_providers: list[PromptProvider] = []
        self._agent_config: AgentConfig = AgentConfig()
        self._turn_spec_builder: TurnSpecBuilder = TurnSpecBuilder()
        self._event_stamper_factory: EventStamperFactory = EventStamperMiddleware
        # Лимит итераций агентского цикла; переопределяется .with_max_iterations().
        self._max_iterations: int = 20

    def with_llm(self, llm: LLMPipeline) -> Self:
        """Готовый LLMPipeline (обязательно; см. LLMPipelineFactory)."""
        self._llm = llm
        return self

    def with_tools(self, service: ToolExecutor) -> Self:
        """Готовый реестр инструментов; mutually-exclusive с use_*-путями."""
        self._tool_executor = service
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

    def tool_executor(self) -> ToolExecutor:
        """Собрать (и закешировать) ToolExecutor без сборки Agent."""
        return self._resolve_tool_executor()

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

    def with_messages(self, service: MessageService) -> Self:
        """Хранилище истории; дефолт — InMemoryMessageService()."""
        self._message_service = service
        return self

    def with_history(self, service: HistoryService) -> Self:
        """Журнал AgentEvent; дефолт — InMemoryHistoryService()."""
        self._history_service = service
        return self

    def with_prompts(self, providers: Iterable[PromptProvider]) -> Self:
        """Провайдеры system-prompt блоков; дефолт — пусто."""
        self._prompt_providers = list(providers)
        return self

    def with_config(self, config: AgentConfig) -> Self:
        """Лимиты агентского лупа; дефолт — AgentConfig()."""
        self._agent_config = config
        return self

    def use_turn_reducer(
        self,
        reducer_or_factory: TurnReducer | TurnReducerFactory,
    ) -> Self:
        """Зарегистрировать стадию TurnSpec.

        Принимает:
        - готовый `TurnReducer` (context-independent), напр.
          `RememberUserQueryReducer()`;
        - фабрику `(AgentContext) -> TurnReducer`, если reducer'у нужен ctx.
        Reducer с тем же `id()` перезатрёт ранее зарегистрированный с этим id.

        Если ни один `use_turn_reducer` / `use_default_turn_reducers` не был
        вызван до `build()`, дефолтный набор подключается автоматически.
        """
        self._turn_spec_builder.add(reducer_or_factory)
        return self

    def with_max_iterations(self, limit: int) -> Self:
        """Лимит итераций агентского цикла. Дефолт 20."""
        if limit < 1:
            msg = (
                f"AgentBuilder.with_max_iterations: limit должен быть >= 1, "
                f"получено {limit}"
            )
            raise ValueError(msg)
        self._max_iterations = limit
        return self

    def use_event_stamper(self, factory: EventStamperFactory) -> Self:
        """Переопределить EventStamper-middleware своей фабрикой.

        Фабрика принимает inner-стрим и возвращает обёртку — passthrough,
        который проставляет envelope-поля (seq / emitted_at / iteration)
        на каждом проходящем AgentEvent. Дефолт — `EventStamperMiddleware`.
        """
        self._event_stamper_factory = factory
        return self

    def use_default_turn_reducers(self) -> Self:
        """Зарегистрировать дефолтный набор TurnSpec'а.

        Состав: model / system_prompt / history / tools / sampling. Зависимости
        (`prompt_providers`, `message_service`, `tool_executor`) разрешаются
        на момент `build()` — порядок вызовов в fluent-цепочке не важен.

        Полезно, когда нужно «дефолт + что-то ещё»: вызови этот метод явно,
        затем `use_turn_reducer(R)`. Если ни один reducer не зарегистрирован,
        `build()` вызовет этот метод автоматически.
        """
        self._turn_spec_builder.add(
            lambda ctx: ModelReducer(ctx.request.model),
        )
        self._turn_spec_builder.add(
            lambda _ctx: SystemPromptReducer(self._prompt_providers),
        )
        self._turn_spec_builder.add(
            lambda _ctx: HistoryReducer(self._message_service),
        )
        self._turn_spec_builder.add(
            lambda _ctx: ToolsReducer(self._resolve_tool_executor()),
        )
        self._turn_spec_builder.add(
            lambda ctx: AgentRequestSamplingReducer(ctx.request.sampling),
        )
        return self

    def agent_config(self) -> AgentConfig:
        """Текущий AgentConfig (нужен для Agent.run)."""
        return self._agent_config

    def build(self) -> Agent:
        """Собрать Agent. ToolContext передаётся per-call через AgentInput."""
        if self._llm is None:
            msg = "AgentBuilder.build: .with_llm(...) обязателен до .build()"
            raise ValueError(msg)

        self._resolve_tool_executor()
        message_service = self._message_service
        history_service = self._history_service

        if self._turn_spec_builder.is_empty():
            self.use_default_turn_reducers()

        chain = self._build_chain(
            llm=self._llm,
            message_writer=message_service,
            history_writer=history_service,
            tool_executor=self._resolve_tool_executor(),
            turn_spec_builder=self._turn_spec_builder,
            event_stamper_factory=self._event_stamper_factory,
            max_iterations=self._max_iterations,
        )
        source = StreamSourceLoop(
            source=chain,
            stop_if=StopOnFinished().or_(StopOnAnyFailure()),
        )
        return Agent(source=source, writer=message_service, reader=message_service)

    def _resolve_tool_executor(self) -> ToolExecutor:
        """Выбрать готовый `ToolExecutor` или собрать из накопленных источников."""
        has_accumulated = (
            bool(self._inline_factories)
            or bool(self._plugin_entries)
            or bool(self._discover_groups)
        )
        if self._tool_executor is not None and has_accumulated:
            msg = (
                "AgentBuilder.build: .with_tools(...) взаимоисключающий "
                "с use_*-путями — задан один маршрут"
            )
            raise ValueError(msg)
        if self._tool_executor is not None:
            return self._tool_executor

        if self._resolved_tool_executor is not None:
            return self._resolved_tool_executor

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
        self._resolved_tool_executor = ToolExecutor.from_sources(sources)
        return self._resolved_tool_executor

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
        llm: LLMPipeline,
        message_writer: MessageWriter,
        history_writer: HistoryWriter,
        tool_executor: ToolExecutor,
        turn_spec_builder: TurnSpecBuilder,
        event_stamper_factory: EventStamperFactory,
        max_iterations: int,
    ) -> StreamSource[AgentContext, AgentEvent]:
        error_router = AgentErrorRouter(message_writer)
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
            lambda inner: IterationCounterMiddleware(inner, max_iterations),
        )
        builder.use(
            lambda inner: ToolExecutionMiddleware(
                inner,
                tool_executor,
                message_writer,
            ),
        )
        builder.use(
            lambda inner: AssistantMessagePersistenceMiddleware(
                inner, message_writer,
            ),
        )
        return builder.terminal(LLMInvokeMiddleware(llm, turn_spec_builder))
