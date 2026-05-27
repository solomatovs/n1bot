"""AgentBuilder — composition root агента, симметричный `LLMBuilder`.

Архитектура:
    AgentBuilder — flat fluent facade. Внутри держит три ортогональных аккумулятора:

      * `_DIRegistry`    — providers / classes / aliases / tools / plugins → Container
      * `_PipelineSpec`  — mandatory-слоты + user middleware → onion-цепочка
      * `_LoopPolicy`    — stop conditions → Specification для StreamSourceLoop

Mandatory-слоты (фиксированный порядок, outer → inner):
    HistoryRecorder → EventStamper → AgentErrorRouter
        → [user middleware в порядке регистрации]
            → ToolExecutor → UserQueryRecorder → terminal

Терминал (по аналогии с `LLMBuilder.build(factory)`) — обязательный
аргумент `build(terminal_cls)`. Все non-`inner` параметры __init__
терминала и middleware резолвятся из Container через DI.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from inspect import Parameter
from typing import Any, Self, get_type_hints

from dishka import Container, Provider, make_container
from dishka.entities.component import Component
from pydantic import BaseModel, ConfigDict

from boba.agent.agent import Agent, AgentContext
from boba.agent.events import AgentEvent
from boba.agent.history import (
    HistoryReader,
    HistoryService,
    HistoryWriter,
    InMemoryHistoryService,
)
from boba.agent.middleware import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    EventStamperMiddleware,
    HistoryRecorderMiddleware,
    LLMPort,
    StopIfContentFilter,
    StopIfLengthReached,
    StopIfReasonStop,
    StopOnAnyFailure,
    ToolExecutionMiddleware,
    UserQueryRecorderMiddleware,
)
from boba.agent.turn.builder import TurnBuilder
from boba.agent.turn.history_view import (
    AllHistoryDialogView,
    CompactHistoryDialogView,
    HistoryDialogView,
)
from boba.llm.builder import LLM, LLMBuilder
from boba.patterns import (
    Specification,
    StreamSource,
    StreamSourceLoop,
)
from boba.provider.openai import OpenAIConfig, use_openai
from boba.settings import ConfigSource, StringList, TomlEnvConfigSource
from boba.tools import (
    DEFAULT_PLUGIN_ENTRY_POINT,
    DuplicateProviderError,
    FromConfig,
    Scope,
    ToolDeclarationError,
)
from boba.tools.adapter import DishkaTool
from boba.tools.decorators import (
    is_provider,
    is_tool,
    provider_scope,
    tool_explicit_name,
)
from boba.tools.domain.ids import ToolSourceId
from boba.tools.framework.registry import (
    StaticToolSource,
    ToolExecutor,
    ToolRegistry,
    ToolSource,
)
from boba.tools.introspect import CallPlan, introspect_callable
from boba.tools.scope import to_dishka_scope

__all__ = ["AgentBuilder", "AgentBuilderConfig", "PluginConfigBase"]

_logger = logging.getLogger(__name__)

_DEFAULT_COMPONENT: str = ""
"""Dishka default-component (app-level services)."""


# --------------------------------------------------------------------------- #
# Конфиги
# --------------------------------------------------------------------------- #


class PluginConfigBase(BaseModel):
    """Meta-config плагина: что framework читает из `[tool.<plugin_name>]`."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    tools: StringList | None = None


class AgentBuilderConfig(BaseModel):
    """Bootstrap-конфиг агрегатор."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Слоты pipeline
# --------------------------------------------------------------------------- #


class _Slot(Enum):
    """Mandatory-позиции в pipeline. Порядок определён в `_OUTER` / `_INNER`."""

    HISTORY_RECORDER = "history_recorder"
    EVENT_STAMPER = "event_stamper"
    ERROR_ROUTER = "error_router"
    TOOL_EXECUTOR = "tool_executor"
    USER_QUERY_RECORDER = "user_query_recorder"


_OUTER: tuple[_Slot, ...] = (
    _Slot.HISTORY_RECORDER,
    _Slot.EVENT_STAMPER,
    _Slot.ERROR_ROUTER,
)
"""Outer envelope: ровно эти три слота, в этом порядке, оборачивают всё внутри.

Их инварианты:
    HistoryRecorder — каждое событие должно попасть в журнал.
    EventStamper    — каждое событие имеет seq/iteration/emitted_at.
    ErrorRouter     — RoutableError превращается в TerminalEvent, не пробрасывается.
"""

_INNER: tuple[_Slot, ...] = (
    _Slot.TOOL_EXECUTOR,
    _Slot.USER_QUERY_RECORDER,
)
"""Inner envelope: эти слоты сидят между user middleware и terminal.

Их инварианты:
    ToolExecutor       — `ToolCallMessage` исполняется (passthrough если tools нет).
    UserQueryRecorder  — `ctx.query` эмитится как `UserQueryReceived` один раз.
"""

_DEFAULT_SLOT_CLASSES: dict[_Slot, type[StreamSource[AgentContext, AgentEvent]]] = {
    _Slot.HISTORY_RECORDER: HistoryRecorderMiddleware,
    _Slot.EVENT_STAMPER: EventStamperMiddleware,
    _Slot.ERROR_ROUTER: AgentErrorRouterMiddleware,
    _Slot.TOOL_EXECUTOR: ToolExecutionMiddleware,
    _Slot.USER_QUERY_RECORDER: UserQueryRecorderMiddleware,
}


# --------------------------------------------------------------------------- #
# DI registrations (data records)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _ProviderEntry:
    fn: Callable[..., Any]
    scope: Scope
    component: str
    plan: CallPlan


@dataclass(frozen=True)
class _ClassEntry:
    cls: type
    provides: type | None
    scope: Scope
    component: str


@dataclass(frozen=True)
class _AliasEntry:
    source: type
    provides: type
    component: str


@dataclass(frozen=True)
class _ToolEntry:
    obj: Any
    component: str
    plan: CallPlan


# --------------------------------------------------------------------------- #
# DI sub-builder
# --------------------------------------------------------------------------- #


class _DIRegistry:
    """Аккумулятор DI-регистраций. `build_container(...)` собирает Dishka Container.

    `replace=True` в register_* перетирает существующую регистрацию того же
    `(provides, component)`, вместо `DuplicateProviderError`. Это явный
    opt-in, чтобы случайные коллизии не маскировались.
    """

    def __init__(self) -> None:
        self._providers: list[_ProviderEntry] = []
        self._classes: list[_ClassEntry] = []
        self._aliases: list[_AliasEntry] = []
        self._tools: list[_ToolEntry] = []
        self._config_source: ConfigSource = TomlEnvConfigSource()

    # ---- config source --------------------------------------------------- #

    def use_config(self, source: ConfigSource) -> Self:
        """Override ConfigSource (TOML/env). Используется для `discover_plugins`."""
        self._config_source = source
        return self

    @property
    def config_source(self) -> ConfigSource:
        return self._config_source

    # ---- register_* ------------------------------------------------------ #

    def register_provider(
        self,
        fn: Callable[..., Any],
        *,
        scope: Scope = Scope.APP,
        component: str = _DEFAULT_COMPONENT,
        replace: bool = False,
    ) -> Self:
        plan = introspect_callable(fn)
        _validate_provider_return(fn, plan)
        if replace:
            self._remove(plan.return_type, component)
        else:
            self._raise_if_taken(plan.return_type, component)
        self._providers.append(
            _ProviderEntry(fn=fn, scope=scope, component=component, plan=plan),
        )
        return self

    def register_class(
        self,
        cls: type,
        *,
        provides: type | None = None,
        scope: Scope = Scope.APP,
        component: str = _DEFAULT_COMPONENT,
        replace: bool = False,
    ) -> Self:
        target = provides if provides is not None else cls
        if replace:
            self._remove(target, component)
        else:
            self._raise_if_taken(target, component)
        self._classes.append(
            _ClassEntry(cls=cls, provides=provides, scope=scope, component=component),
        )
        return self

    def register_instance(
        self,
        instance: Any,
        *,
        provides: type | None = None,
        scope: Scope = Scope.APP,
        component: str = _DEFAULT_COMPONENT,
        replace: bool = False,
    ) -> Self:
        target = provides if provides is not None else type(instance)

        def _factory() -> Any:
            return instance

        _factory.__annotations__ = {"return": target}
        _factory.__name__ = f"_provide_{target.__name__}"
        return self.register_provider(
            _factory,
            scope=scope,
            component=component,
            replace=replace,
        )

    def register_alias(
        self,
        *,
        source: type,
        provides: type,
        component: str = _DEFAULT_COMPONENT,
        replace: bool = False,
    ) -> Self:
        if replace:
            self._remove(provides, component)
        else:
            self._raise_if_taken(provides, component)
        self._aliases.append(
            _AliasEntry(source=source, provides=provides, component=component),
        )
        return self

    # tools / plugins

    def use_tools(self, items: Iterable[Any]) -> Self:
        for item in items:
            self._absorb(item, "inline")
        return self

    def use_plugin(self, module: object) -> Self:
        self._scan_module(module, allowlist=None)
        return self

    def discover_plugins(self, entry_point: str) -> Self:
        for ep in importlib.metadata.entry_points(group=entry_point):
            raw = self._config_source.for_path(("tool", ep.name))
            meta = PluginConfigBase.model_validate(raw)
            if not meta.enable:
                continue
            try:
                module = ep.load()
            except Exception as exc:
                _logger.warning(
                    "plugin entry-point %r load failed: %s: %s; skipped",
                    ep.name,
                    type(exc).__name__,
                    exc,
                )
                continue
            self._scan_module(module, allowlist=meta.tools)
        return self

    # container assembly

    def build_container(self) -> Container:
        """Резолвит FromConfig-зависимости и собирает Container."""
        configs = self._instantiate_configs()
        config_types = set(configs.keys())

        components: dict[str, list[_ProviderEntry]] = {}
        for p in self._providers:
            components.setdefault(p.component, []).append(p)
        for t in self._tools:
            components.setdefault(t.component, [])
        for c in self._classes:
            components.setdefault(c.component, [])
        for a in self._aliases:
            components.setdefault(a.component, [])

        default_entries = components.pop(_DEFAULT_COMPONENT, [])
        default_classes = [
            c for c in self._classes if c.component == _DEFAULT_COMPONENT
        ]
        default_aliases = [
            a for a in self._aliases if a.component == _DEFAULT_COMPONENT
        ]

        default_provider = self._build_default_provider(
            default_entries,
            default_classes,
            default_aliases,
            config_types,
        )
        plugin_providers = [
            self._build_plugin_provider(name, entries)
            for name, entries in components.items()
        ]
        return make_container(default_provider, *plugin_providers, context=configs)

    @property
    def tools(self) -> list[_ToolEntry]:
        """Read-only снимок зарегистрированных tool'ов."""
        return list(self._tools)

    @property
    def providers(self) -> list[_ProviderEntry]:
        """Read-only снимок зарегистрированных provider-фабрик."""
        return list(self._providers)

    # internals

    def _absorb(self, obj: Any, component: str) -> None:
        if is_provider(obj):
            self.register_provider(
                obj,
                scope=provider_scope(obj),
                component=component,
            )
        elif is_tool(obj):
            plan = introspect_callable(obj)
            self._tools.append(_ToolEntry(obj=obj, component=component, plan=plan))
        else:
            msg = (
                f"AgentBuilder: {obj!r} — не помечен ни как @tool, "
                f"ни как @provides; нечего регистрировать"
            )
            raise ToolDeclarationError(msg)

    def _scan_module(self, module: object, *, allowlist: list[str] | None) -> None:
        component = getattr(module, "__name__", repr(module))
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module, attr_name, None)
            if obj is None:
                continue
            if is_tool(obj):
                if allowlist is not None and _tool_wire_name(obj) not in allowlist:
                    continue
                self._absorb(obj, component)
            elif is_provider(obj):
                self._absorb(obj, component)

    def _raise_if_taken(self, target: type, component: str) -> None:
        for p in self._providers:
            if p.component == component and p.plan.return_type is target:
                msg = (
                    f"component {component!r}: тип {target!r} уже "
                    f"зарегистрирован provider'ом"
                )
                raise DuplicateProviderError(msg)
        for c in self._classes:
            existing = c.provides if c.provides is not None else c.cls
            if c.component == component and existing is target:
                msg = (
                    f"component {component!r}: тип {target!r} уже "
                    f"зарегистрирован классом"
                )
                raise DuplicateProviderError(msg)
        for a in self._aliases:
            if a.component == component and a.provides is target:
                msg = (
                    f"component {component!r}: тип {target!r} уже "
                    f"зарегистрирован alias'ом"
                )
                raise DuplicateProviderError(msg)

    def _remove(self, target: type, component: str) -> None:
        self._providers = [
            p
            for p in self._providers
            if not (p.component == component and p.plan.return_type is target)
        ]
        self._classes = [
            c
            for c in self._classes
            if not (
                c.component == component
                and (c.provides if c.provides is not None else c.cls) is target
            )
        ]
        self._aliases = [
            a
            for a in self._aliases
            if not (a.component == component and a.provides is target)
        ]

    def _instantiate_configs(self) -> dict[type, object]:
        cfg_types: set[type] = set()
        for p in self._providers:
            for dep in p.plan.di_deps:
                if isinstance(dep.marker, FromConfig):
                    cfg_types.add(dep.target_type)
        for t in self._tools:
            for dep in t.plan.di_deps:
                if isinstance(dep.marker, FromConfig):
                    cfg_types.add(dep.target_type)
        return {ct: self._load_config(ct) for ct in cfg_types}

    def _load_config(self, cfg_type: type) -> object:
        path = _config_path_for(cfg_type)
        data = self._config_source.for_path(path)
        return cfg_type.model_validate(data)

    @staticmethod
    def _build_default_provider(
        entries: list[_ProviderEntry],
        classes: list[_ClassEntry],
        aliases: list[_AliasEntry],
        config_types: set[type],
    ) -> Provider:
        provider = Provider(scope=to_dishka_scope(Scope.APP))
        for cfg_type in config_types:
            provider.from_context(provides=cfg_type, scope=to_dishka_scope(Scope.APP))
        for entry in entries:
            provider.provide(source=entry.fn, scope=to_dishka_scope(entry.scope))
        for ce in classes:
            provider.provide(
                source=ce.cls,
                provides=ce.provides,
                scope=to_dishka_scope(ce.scope),
            )
        for ae in aliases:
            provider.alias(source=ae.source, provides=ae.provides)
        return provider

    def _build_plugin_provider(
        self,
        component_name: str,
        entries: list[_ProviderEntry],
    ) -> Provider:
        local_types = {e.plan.return_type for e in entries}
        used_types: set[type] = set()
        for e in entries:
            for dep in e.plan.di_deps:
                used_types.add(dep.target_type)
        for t in self._tools:
            if t.component != component_name:
                continue
            for dep in t.plan.di_deps:
                used_types.add(dep.target_type)
        alias_types = used_types - local_types

        provider = Provider(
            scope=to_dishka_scope(Scope.APP),
            component=Component(component_name),
        )
        for ct in alias_types:
            provider.alias(source=ct, component=Component(_DEFAULT_COMPONENT))
        for entry in entries:
            provider.provide(source=entry.fn, scope=to_dishka_scope(entry.scope))
        return provider


# --------------------------------------------------------------------------- #
# Pipeline sub-builder
# --------------------------------------------------------------------------- #


class _PipelineSpec:
    """Mandatory-слоты + user middleware → onion-цепочка над terminal.

    Сборка фиксирована: `_OUTER → middlewares → _INNER → terminal`.
    Слот переопределяется через `set_slot(slot, cls)`; user middleware
    добавляется через `use_middleware(cls)` в порядке регистрации.
    """

    def __init__(self) -> None:
        self._slots: dict[_Slot, type] = dict(_DEFAULT_SLOT_CLASSES)
        self._middlewares: list[type] = []

    def set_slot(self, slot: _Slot, cls: type) -> Self:
        self._slots[slot] = cls
        return self

    def use_middleware(self, cls: type) -> Self:
        self._middlewares.append(cls)
        return self

    def build(
        self,
        terminal_cls: type,
        container: Container,
    ) -> StreamSource[AgentContext, AgentEvent]:
        """Собрать onion-цепочку. inner-most → outer-most:

        terminal()                              ← аргумент build()
        UserQueryRecorder(terminal)             ← _INNER (reverse iterate)
        ToolExecutor(UserQueryRecorder(...))
        <user middleware in reverse order>      ← reversed self._middlewares
        AgentErrorRouter(...)                   ← _OUTER (reverse iterate)
        EventStamper(...)
        HistoryRecorder(...)                    ← outermost
        """
        chain: StreamSource[AgentContext, AgentEvent] = _construct(
            terminal_cls,
            container,
            with_inner=False,
        )

        for slot in reversed(_INNER):
            chain = _construct(
                self._slots[slot], container, with_inner=True, inner=chain
            )

        for mw_cls in reversed(self._middlewares):
            chain = _construct(mw_cls, container, with_inner=True, inner=chain)

        for slot in reversed(_OUTER):
            chain = _construct(
                self._slots[slot], container, with_inner=True, inner=chain
            )

        return chain


# --------------------------------------------------------------------------- #
# Loop policy
# --------------------------------------------------------------------------- #

_DEFAULT_STOPS: tuple[Specification[tuple[AgentContext, AgentEvent]], ...] = (
    StopIfReasonStop(),
    StopIfLengthReached(),
    StopIfContentFilter(),
    StopOnAnyFailure(),
)
"""Дефолтные стоп-условия. Применяются всегда поверх пользовательских."""


class _LoopPolicy:
    """Аккумулятор `Specification[tuple[AgentContext, AgentEvent]]` (stop_if)."""

    def __init__(self) -> None:
        self._extra: list[Specification[tuple[AgentContext, AgentEvent]]] = []

    def stop_if(self, spec: Specification[tuple[AgentContext, AgentEvent]]) -> Self:
        self._extra.append(spec)
        return self

    def build_spec(self) -> Specification[tuple[AgentContext, AgentEvent]]:
        spec = _DEFAULT_STOPS[0]
        for s in _DEFAULT_STOPS[1:]:
            spec = spec.or_(s)
        for s in self._extra:
            spec = spec.or_(s)
        return spec


# --------------------------------------------------------------------------- #
# AgentBuilder (flat facade)
# --------------------------------------------------------------------------- #


def _openai_provider() -> LLM:
    """Дефолтная сборка LLM — OpenAI-совместимый terminal без observers."""
    return LLMBuilder().build(use_openai(OpenAIConfig()))


class AgentBuilder:
    """
    Fluent facade

    Sub-builder доступны через `.di`, `.pipeline`, `.loop`
    """

    def __init__(self) -> None:
        self.di = _DIRegistry()
        self.pipeline = _PipelineSpec()
        self.loop = _LoopPolicy()
        self._turn: TurnBuilder | None = None
        self._error_router: AgentErrorRouter = AgentErrorRouter()
        self._compact_max_messages: int | None = None

        # Дефолты:
        self.use_history(InMemoryHistoryService)
        self.di.register_provider(_openai_provider)
        self.di.register_instance(
            AgentBuilderConfig(),
            provides=AgentBuilderConfig,
        )

    def use_llm(self, llm: LLM) -> Self:
        """Override default LLM уже собранным экземпляром."""
        self.di.register_instance(llm, provides=LLM, replace=True)
        return self

    def use_history(self, cls: type[HistoryService]) -> Self:
        """Заменить `HistoryService` класс. Alias'ы reader/writer привязаны к нему."""
        self.di.register_class(cls, provides=HistoryService, replace=True)
        self.di.register_alias(
            source=HistoryService,
            provides=HistoryReader,
            replace=True,
        )
        self.di.register_alias(
            source=HistoryService,
            provides=HistoryWriter,
            replace=True,
        )
        return self

    def use_turn(self, turn: TurnBuilder) -> Self:
        """Описание следующего хода. Обязательно до `.build()`."""
        self._turn = turn
        return self

    def use_compact_history(self, max_messages: int) -> Self:
        """Подключить `CompactHistoryDialogView` как дефолтный view для turn.

        Прошлые `request_id` сжимаются до user+финальный text-ответ,
        а скользящее окно оставляет последние `max_messages` сообщений.
        Без вызова метода дефолт — `AllHistoryDialogView` (вся история).
        Явный `TurnBuilder.with_history_view(...)` имеет приоритет.
        """
        self._compact_max_messages = max_messages
        return self

    def use_tools(self, items: Iterable[Any]) -> Self:
        self.di.use_tools(items)
        return self

    def use_plugin(self, module: object) -> Self:
        self.di.use_plugin(module)
        return self

    def discover_plugins(
        self,
        entry_point: str = DEFAULT_PLUGIN_ENTRY_POINT,
    ) -> Self:
        self.di.discover_plugins(entry_point)
        return self

    def use_config(self, source: ConfigSource) -> Self:
        """Override ConfigSource (TOML/env). Используется для `discover_plugins`."""
        self.di.use_config(source)
        return self

    def register_provider(
        self,
        fn: Callable[..., Any],
        *,
        scope: Scope = Scope.APP,
        component: str = _DEFAULT_COMPONENT,
        replace: bool = False,
    ) -> Self:
        self.di.register_provider(fn, scope=scope, component=component, replace=replace)
        return self

    def register_class(
        self,
        cls: type,
        *,
        provides: type | None = None,
        scope: Scope = Scope.APP,
        component: str = _DEFAULT_COMPONENT,
        replace: bool = False,
    ) -> Self:
        self.di.register_class(
            cls,
            provides=provides,
            scope=scope,
            component=component,
            replace=replace,
        )
        return self

    def register_instance(
        self,
        instance: Any,
        *,
        provides: type | None = None,
        scope: Scope = Scope.APP,
        component: str = _DEFAULT_COMPONENT,
        replace: bool = False,
    ) -> Self:
        self.di.register_instance(
            instance,
            provides=provides,
            scope=scope,
            component=component,
            replace=replace,
        )
        return self

    def register_alias(
        self,
        *,
        source: type,
        provides: type,
        component: str = _DEFAULT_COMPONENT,
        replace: bool = False,
    ) -> Self:
        self.di.register_alias(
            source=source,
            provides=provides,
            component=component,
            replace=replace,
        )
        return self

    def use_history_recorder(self, cls: type) -> Self:
        self.pipeline.set_slot(_Slot.HISTORY_RECORDER, cls)
        return self

    def use_event_stamper(self, cls: type) -> Self:
        self.pipeline.set_slot(_Slot.EVENT_STAMPER, cls)
        return self

    def use_error_router(self, cls: type) -> Self:
        self.pipeline.set_slot(_Slot.ERROR_ROUTER, cls)
        return self

    def use_tool_executor(self, cls: type) -> Self:
        self.pipeline.set_slot(_Slot.TOOL_EXECUTOR, cls)
        return self

    def use_user_query_recorder(self, cls: type) -> Self:
        self.pipeline.set_slot(_Slot.USER_QUERY_RECORDER, cls)
        return self

    def use_middleware(self, cls: type) -> Self:
        """Добавить optional middleware в цепочку (между OUTER и INNER слотами)."""
        self.pipeline.use_middleware(cls)
        return self

    def stop_if(
        self,
        spec: Specification[tuple[AgentContext, AgentEvent]],
    ) -> Self:
        """Добавить пользовательское stop-условие (additive поверх дефолтов)."""
        self.loop.stop_if(spec)
        return self

    def build(self, terminal: type = LLMPort) -> Agent:
        """Собрать Agent. `terminal` — класс terminal-stage (дефолт `LLMPort`).

        `.use_turn(...)` обязателен. Перед сборкой Container регистрируются
        internal-сервисы (TurnBuilder, AgentErrorRouter, AllHistoryDialogView,
        ToolExecutor late-binding).
        """
        if self._turn is None:
            msg = "AgentBuilder.build: .use_turn(...) обязателен до .build()"
            raise ValueError(msg)

        self._register_internals()

        registry_cell: list[ToolRegistry] = []

        def _provide_tool_executor() -> ToolExecutor:
            return registry_cell[0].executor()

        self.di.register_provider(_provide_tool_executor, scope=Scope.APP)

        container = self.di.build_container()

        registry = _build_registry(self.di.tools, container)
        registry_cell.append(registry)

        if not self._turn.has_history_view():
            self._turn.with_history_view(self._default_history_view(container))
        if not self._turn.has_tool_catalog():
            self._turn.with_tool_catalog(registry.catalog())

        chain = self.pipeline.build(terminal, container)
        source = StreamSourceLoop(source=chain, stop_if=self.loop.build_spec())
        return Agent(source=source, container=container)

    # ---- internals ------------------------------------------------------- #

    def _register_internals(self) -> None:
        """
        Зарегистрировать core-сервисы (TurnBuilder, ErrorRouter, AllHistoryDialogView)
        """
        self.di.register_provider(self._provide_turn)
        self.di.register_provider(self._provide_error_router)
        self.di.register_class(AllHistoryDialogView)

    def _default_history_view(self, container: Container) -> HistoryDialogView:
        if self._compact_max_messages is not None:
            return CompactHistoryDialogView(
                container.get(HistoryReader),
                max_messages=self._compact_max_messages,
            )
        return container.get(AllHistoryDialogView)

    def _provide_turn(self) -> TurnBuilder:
        if self._turn is None:
            raise RuntimeError(
                "_provide_turn called before use_turn() — invariant broken"
            )
        return self._turn

    def _provide_error_router(self) -> AgentErrorRouter:
        return self._error_router


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _construct(
    cls: type,
    container: Container,
    *,
    with_inner: bool,
    inner: StreamSource[AgentContext, AgentEvent] | None = None,
) -> StreamSource[AgentContext, AgentEvent]:
    """Сконструировать stage: `inner` позиционно (если with_inner), остальное — DI."""
    exclude = {"self", "inner"} if with_inner else {"self"}
    kwargs = _resolve_init_kwargs(cls, container, exclude=exclude)
    if with_inner:
        return cls(inner, **kwargs)
    return cls(**kwargs)


def _resolve_init_kwargs(
    cls: type,
    container: Container,
    exclude: set[str],
) -> dict[str, Any]:
    """Резолвит non-`exclude` параметры `__init__` через `container.get(T)`."""
    sig = inspect.signature(cls.__init__)
    hints = get_type_hints(cls.__init__)
    kwargs: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name in exclude:
            continue
        annotation = hints.get(name, param.annotation)
        if annotation is Parameter.empty:
            raise TypeError(f"{cls.__name__}: param {name!r} without annotation")
        kwargs[name] = container.get(annotation)
    return kwargs


def _validate_provider_return(fn: Callable[..., Any], plan: CallPlan) -> None:
    if plan.return_type is Parameter.empty or plan.return_type is None:
        msg = (
            f"@provides function {getattr(fn, '__name__', repr(fn))!r}: "
            f"return type обязателен — это тип, под которым служба "
            f"регистрируется в DI"
        )
        raise ToolDeclarationError(msg)


def _config_path_for(cfg_type: type) -> tuple[str, ...]:
    mc = getattr(cfg_type, "model_config", {})
    section = mc.get("config_path", "") if isinstance(mc, dict) else ""
    if isinstance(section, str):
        return tuple(s for s in section.split(".") if s)
    return tuple(section)


def _build_registry(tools: Iterable[_ToolEntry], container: Container) -> ToolRegistry:
    """Обернуть `@tool` callables в `DishkaTool` и собрать `ToolRegistry`."""
    by_component: dict[str, list[_ToolEntry]] = {}
    for t in tools:
        by_component.setdefault(t.component, []).append(t)

    sources: list[ToolSource] = []
    for component_name, tool_entries in by_component.items():
        sid = ToolSourceId(_component_to_source_id(component_name))
        dishka_tools = [
            DishkaTool(
                target=_resolve_callable(t.obj),
                plan=t.plan,
                container=container,
                component=component_name,
                source_id=sid,
            )
            for t in tool_entries
        ]
        sources.append(StaticToolSource(sid, dishka_tools))

    return ToolRegistry.from_sources(sources)


def _resolve_callable(obj: Any) -> Any:
    if inspect.isclass(obj):
        return obj()
    return obj


def _tool_wire_name(obj: Any) -> str:
    explicit = tool_explicit_name(obj)
    if explicit is not None:
        return explicit
    return getattr(obj, "__name__", None) or type(obj).__name__


def _component_to_source_id(component_name: str) -> str:
    sanitized = re.compile(r"[^A-Za-z0-9_-]").sub("_", component_name)
    if not sanitized or not sanitized[0].isalnum():
        sanitized = "p_" + sanitized
    return sanitized
