"""AgentBuilder — fluent-фасад для сборки Agent с разумными дефолтами."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import Any, ClassVar, Self

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
from boba.agent.orchestrator import Agent, AgentConfig, AgentContext
from boba.agent.prompt import PromptProvider
from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigSource
from boba.llm.events import LLMEvent
from boba.llm.models import LLMContext
from boba.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
    StreamSourceLoop,
)
from boba.plugin import ExtensionContext, Plugin, config_path
from boba.tools.domain import ToolContext, ToolResultVisitor, ToolSourceId
from boba.tools.framework import (
    StaticToolSource,
    ToolDecoratorFactory,
    ToolSource,
    ToolsService,
)


class AgentBuilder:
    """Fluent-фасад: собирает Agent с дефолтной middleware-цепью."""

    INLINE_SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("inline")

    def __init__(self) -> None:
        self._llm_source: StreamSource[LLMContext, LLMEvent] | None = None
        self._tools_service: ToolsService | None = None
        self._inline_factories: list[ToolDecoratorFactory] = []
        self._config_sources: list[ConfigSource] = []
        self._plugin_entries: list[
            tuple[type[Plugin[Any, ToolSource]], ConfigSource | Any | None]
        ] = []
        self._tool_result_visitor: ToolResultVisitor[str] | None = None
        self._message_service: MessageService | None = None
        self._prompt_providers: list[PromptProvider] = []
        self._agent_config: AgentConfig = AgentConfig()

    def with_llm(self, source: StreamSource[LLMContext, LLMEvent]) -> Self:
        """LLM-источник (обязательно)."""
        self._llm_source = source
        return self

    def with_tools(self, service: ToolsService) -> Self:
        """Готовый реестр инструментов; mutually-exclusive с use_*-путями."""
        self._tools_service = service
        return self

    def use_tools(self, factories: Iterable[ToolDecoratorFactory]) -> Self:
        """Добавить `@tool`-функции под общим source_id `INLINE_SOURCE_ID`."""
        self._inline_factories.extend(factories)
        return self

    def use_config_source(self, source: ConfigSource) -> Self:
        """Зарегистрировать ConfigSource для материализации plugin-конфигов."""
        self._config_sources.append(source)
        return self

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

    def build(
        self,
        *,
        tool_ctx: ToolContext,
    ) -> Agent:
        """Собрать Agent. tool_ctx — per-call DI, не часть билдера."""
        if self._llm_source is None:
            msg = "AgentBuilder.build: .with_llm(...) обязателен до .build()"
            raise ValueError(msg)
        if self._tool_result_visitor is None:
            msg = (
                "AgentBuilder.build: .with_tool_result_visitor(...) "
                "обязателен до .build()"
            )
            raise ValueError(msg)

        tools_service = self._resolve_tools_service()

        message_service = self._message_service or InMemoryMessageService()

        chain = self._build_chain(
            llm_source=self._llm_source,
            tools_service=tools_service,
            visitor=self._tool_result_visitor,
            prompt_providers=self._prompt_providers,
            message_service=message_service,
            tool_ctx=tool_ctx,
        )
        source = StreamSourceLoop(
            source=chain,
            stop_if=StopOnFinished().or_(StopOnAnyFailure()),
        )
        return Agent(source=source, writer=message_service, reader=message_service)

    def _resolve_tools_service(self) -> ToolsService:
        """Выбрать готовый `ToolsService` или собрать из накопленных источников."""
        has_accumulated = (
            bool(self._inline_factories)
            or bool(self._plugin_entries)
            or bool(self._config_sources)
        )
        if self._tools_service is not None and has_accumulated:
            msg = (
                "AgentBuilder.build: .with_tools(...) взаимоисключающий "
                "с use_*-путями — задан один маршрут"
            )
            raise ValueError(msg)
        if self._tools_service is not None:
            return self._tools_service

        if not self._inline_factories and not self._plugin_entries:
            msg = (
                "AgentBuilder.build: задайте инструменты через "
                ".with_tools(...), .use_tools(...) или .use_tools_plugin(...)"
            )
            raise ValueError(msg)

        shared_bundle = (
            ConfigBundle.from_sources(self._config_sources)
            if self._config_sources
            else None
        )

        sources: list[ToolSource] = []
        if self._inline_factories:
            sid = self.INLINE_SOURCE_ID
            sources.append(
                StaticToolSource(
                    sid, [f.build(sid) for f in self._inline_factories],
                ),
            )
        ctx = ExtensionContext()
        for plugin_cls, config in self._plugin_entries:
            cfg = self._materialize_plugin_config(plugin_cls, config, shared_bundle)
            sources.extend(plugin_cls.build(cfg, ctx))
        return ToolsService.from_sources(sources)

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
            return shared_bundle.materialize(
                config_path(plugin_cls.NAME), plugin_cls.config(),
            )
        if isinstance(config, ConfigSource):
            return ConfigBundle.from_sources([config]).materialize(
                config_path(plugin_cls.NAME), plugin_cls.config(),
            )
        return config

    @staticmethod
    def _build_chain(  # noqa: PLR0913
        *,
        llm_source: StreamSource[LLMContext, LLMEvent],
        tools_service: ToolsService,
        visitor: ToolResultVisitor[str],
        prompt_providers: list[PromptProvider],
        message_service: MessageService,
        tool_ctx: ToolContext,
    ) -> StreamSource[AgentContext, AgentEvent]:
        writer: MessageWriter = message_service
        error_router = AgentErrorRouter(writer)
        builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
        builder.use(lambda inner: AgentErrorRouterMiddleware(inner, error_router))
        builder.use(IterationCounterMiddleware)
        builder.use(
            lambda inner: ToolExecutionMiddleware(
                inner,
                tools_service,
                tool_ctx,
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
                tools_service,
                message_service,
            ),
        )
