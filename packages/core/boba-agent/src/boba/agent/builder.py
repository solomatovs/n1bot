"""AgentBuilder — fluent-фасад для сборки Agent с разумными дефолтами."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import Any, ClassVar, Self, get_args, get_origin

from boba.agent.events import AgentEvent
from boba.agent.messages import InMemoryMessageService, MessageService, MessageWriter
from boba.agent.middleware import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    AssistantMessagePersistenceMiddleware,
    IterationCounterMiddleware,
    LLMInvokeMiddleware,
    StopOnAnyFailure,
    StopOnFinished,
    ToolExecutionMiddleware,
)
from boba.agent.middleware.llm import LLMSource
from boba.agent.orchestrator import Agent, AgentConfig, AgentContext
from boba.agent.prompt import PromptProvider
from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigSource
from boba.config.source.cli import CliSource
from boba.config.source.env import EnvFileSource, EnvSource
from boba.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
    StreamSourceLoop,
)
from boba.plugin import ExtensionContext, Plugin, config_path, is_enabled
from boba.plugin.discovery import DEFAULT_PLUGIN_ENTRY_POINT_GROUP, discover_plugins
from boba.tools.domain import ToolResultVisitor, ToolSourceId
from boba.tools.framework import (
    StaticToolSource,
    ToolDecoratorFactory,
    ToolExecutor,
    ToolSource,
)


class AgentBuilder:
    """Fluent-фасад: собирает Agent с дефолтной middleware-цепью."""

    INLINE_SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("inline")

    def __init__(self) -> None:
        self._llm_source: LLMSource | None = None
        self._tool_executor: ToolExecutor | None = None
        self._inline_factories: list[ToolDecoratorFactory] = []
        self._config_sources: list[ConfigSource] = []
        self._bundle: ConfigBundle | None = None
        self._plugin_entries: list[
            tuple[type[Plugin[Any, ToolSource]], ConfigSource | Any | None]
        ] = []
        self._discover_groups: list[str] = []
        self._extensions: dict[type, object] = {}
        self._resolved_tool_executor: ToolExecutor | None = None
        self._tool_result_visitor: ToolResultVisitor[str] | None = None
        self._message_service: MessageService | None = None
        self._prompt_providers: list[PromptProvider] = []
        self._agent_config: AgentConfig = AgentConfig()

    def with_llm(self, source: LLMSource) -> Self:
        """Готовый LLM-источник (обязательно)."""
        self._llm_source = source
        return self

    def with_tools(self, service: ToolExecutor) -> Self:
        """Готовый реестр инструментов; mutually-exclusive с use_*-путями."""
        self._tool_executor = service
        return self

    def use_tools(self, factories: Iterable[ToolDecoratorFactory]) -> Self:
        """Добавить `@tool`-функции под общим source_id `INLINE_SOURCE_ID`."""
        self._inline_factories.extend(factories)
        return self

    def use_config_source(self, source: ConfigSource) -> Self:
        """Зарегистрировать ConfigSource для общего `ConfigBundle` билдера."""
        if self._bundle is not None:
            msg = (
                "AgentBuilder.use_config_source: ConfigBundle уже зафиксирован "
                "вызовом .bundle()/.build() — новые source'ы добавлять нельзя"
            )
            raise ValueError(msg)
        self._config_sources.append(source)
        return self

    def use_cli(self, argv: Iterable[str] | None = None) -> Self:
        """Шорткат: зарегистрировать стандартный `CliSource()`."""
        return self.use_config_source(
            CliSource(list(argv)) if argv is not None else CliSource(),
        )

    def use_env(self) -> Self:
        """Шорткат: зарегистрировать стандартный `EnvSource()`."""
        return self.use_config_source(EnvSource())

    def use_env_file(self) -> Self:
        """Шорткат: зарегистрировать стандартный `EnvFileSource()`."""
        return self.use_config_source(EnvFileSource())

    def bundle(self) -> ConfigBundle:
        """Собрать и зафиксировать `ConfigBundle` из накопленных source'ов."""
        if self._bundle is None:
            self._bundle = ConfigBundle.from_sources(self._config_sources)
        return self._bundle

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
        config: ConfigSource | Any | None = None,
    ) -> Self:
        """Добавить tool-плагин.

        `plugin` — класс `Plugin` или строка `module:attr`.
        `config`:
        - готовый DTO — пробрасывается as-is;
        - `ConfigSource` — материализуется как локальный bundle;
        - `None` — материализуется из накопленных `use_config_source(...)`.
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

    def with_tool_result_visitor(self, visitor: ToolResultVisitor[str]) -> Self:
        """Сериализация результата tool'а в строку для LLM (обязательно)."""
        self._tool_result_visitor = visitor
        return self

    def with_messages(self, service: MessageService) -> Self:
        """Хранилище истории; дефолт — InMemoryMessageService()."""
        self._message_service = service
        return self

    def with_prompts(self, providers: Iterable[PromptProvider]) -> Self:
        """Провайдеры system-prompt блоков; дефолт — пусто."""
        self._prompt_providers = list(providers)
        return self

    def with_config(self, config: AgentConfig) -> Self:
        """Лимиты агентского лупа; дефолт — AgentConfig()."""
        self._agent_config = config
        return self

    def agent_config(self) -> AgentConfig:
        """Текущий AgentConfig (нужен для Agent.run)."""
        return self._agent_config

    def build(self) -> Agent:
        """Собрать Agent. ToolContext передаётся per-call через AgentInput."""
        if self._llm_source is None:
            msg = "AgentBuilder.build: .with_llm(...) обязателен до .build()"
            raise ValueError(msg)
        if self._tool_result_visitor is None:
            msg = (
                "AgentBuilder.build: .with_tool_result_visitor(...) "
                "обязателен до .build()"
            )
            raise ValueError(msg)

        tool_executor = self._resolve_tool_executor()

        message_service = self._message_service or InMemoryMessageService()

        chain = self._build_chain(
            llm_source=self._llm_source,
            tool_executor=tool_executor,
            visitor=self._tool_result_visitor,
            prompt_providers=self._prompt_providers,
            message_service=message_service,
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
            or bool(self._config_sources)
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

        has_tools = (
            bool(self._inline_factories)
            or bool(self._plugin_entries)
            or bool(self._discover_groups)
        )
        if not has_tools:
            msg = (
                "AgentBuilder.build: задайте инструменты через "
                ".with_tools(...), .use_tools(...), .use_tools_plugin(...) "
                "или .use_tools_plugins_discovered(...)"
            )
            raise ValueError(msg)

        shared_bundle = self.bundle()

        discovered: list[tuple[type[Plugin[Any, ToolSource]], None]] = []
        for group in self._discover_groups:
            for cls in discover_plugins(group):
                if is_enabled(shared_bundle, config_path(cls.NAME)):
                    discovered.append((cls, None))

        sources: list[ToolSource] = []
        if self._inline_factories:
            sid = self.INLINE_SOURCE_ID
            sources.append(
                StaticToolSource(
                    sid, [f.build(sid) for f in self._inline_factories],
                ),
            )
        ctx = ExtensionContext(self._extensions)
        for plugin_cls, config in (*self._plugin_entries, *discovered):
            cfg = self._materialize_plugin_config(plugin_cls, config, shared_bundle)
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
        config: ConfigSource | Any | None,
        shared_bundle: ConfigBundle | None,
    ) -> Any:
        """`config` → DTO; None → shared bundle, ConfigSource → локальный bundle."""
        if config is None:
            if shared_bundle is None:
                msg = (
                    f"AgentBuilder: плагин "
                    f"{plugin_cls.NAME.to_wire()!r} вызван без config, "
                    f"но ни один ConfigSource не зарегистрирован через "
                    f".use_config_source(...)"
                )
                raise ValueError(msg)
            return shared_bundle.get(
                AgentBuilder._resolve_plugin_config_type(plugin_cls),
                config_path(plugin_cls.NAME),
            )
        if isinstance(config, ConfigSource):
            return ConfigBundle.from_sources([config]).get(
                AgentBuilder._resolve_plugin_config_type(plugin_cls),
                config_path(plugin_cls.NAME),
            )
        return config

    @staticmethod
    def _resolve_plugin_config_type(
        plugin_cls: type[Plugin[Any, ToolSource]],
    ) -> type:
        """Извлечь TConfig из объявления `class XPlugin(Plugin[XConfig, ...])`."""
        for klass in plugin_cls.__mro__:
            for base in getattr(klass, "__orig_bases__", ()):
                if get_origin(base) is Plugin:
                    targs = get_args(base)
                    if targs and isinstance(targs[0], type):
                        return targs[0]
        msg = (
            f"{plugin_cls.__name__}: не удалось определить TConfig; "
            f"плагин должен наследовать `Plugin[XConfig, XToolSource]` "
            f"с конкретным dataclass-типом."
        )
        raise TypeError(msg)

    @staticmethod
    def _build_chain(
        *,
        llm_source: LLMSource,
        tool_executor: ToolExecutor,
        visitor: ToolResultVisitor[str],
        prompt_providers: list[PromptProvider],
        message_service: MessageService,
    ) -> StreamSource[AgentContext, AgentEvent]:
        writer: MessageWriter = message_service
        error_router = AgentErrorRouter(writer)
        builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
        builder.use(lambda inner: AgentErrorRouterMiddleware(inner, error_router))
        builder.use(IterationCounterMiddleware)
        builder.use(
            lambda inner: ToolExecutionMiddleware(
                inner,
                tool_executor,
                writer,
                visitor,
            ),
        )
        builder.use(
            lambda inner: AssistantMessagePersistenceMiddleware(inner, writer),
        )
        return builder.terminal(
            LLMInvokeMiddleware(
                llm_source,
                prompt_providers,
                tool_executor,
                message_service,
            ),
        )
