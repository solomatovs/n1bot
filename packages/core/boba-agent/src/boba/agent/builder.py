"""AgentBuilder — composition root агента.

Владеет Dishka `Container` — общим DI-реестром всех служб агента,
который собирается на `.build()` и передаётся в `Agent`. Container
наполняется через единую точку — `register_provider(...)`.

Tool registration API:

- `register_provider(fn, *, scope=Scope.APP, component="")` — единственный
  способ добавить factory в DI. Используется и из user-кода (app-level),
  и внутренне из `use_tools/use_plugin` (с component=имя_плагина).

- `use_tools([...])` — inline список `@tool`/`@provides` callables.
  `@provides` маршрутизируется через `register_provider(..., component="inline")`,
  `@tool` копится для последующей обёртки в `DishkaTool`.

- `use_plugin(module)` — то же что `use_tools`, но обходит атрибуты
  модуля. component'ом плагина становится `module.__name__`.

- `use_plugins(group=...)` — entry-points discovery: для каждого модуля
  из group вызывает `use_plugin`.

хел`_middlewares: list[type]`, для каждого класса резолвит non-`inner`
параметры `__init__` через `container.get(T)` и конструирует. Никаких
явных lambdoй или ручной wiring'и зависимостей — единый механизм
резолюции.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from inspect import Parameter
from typing import Any, Self, get_type_hints

from dishka import Container, Provider, make_container
from dishka.entities.component import Component
from pydantic import BaseModel, ConfigDict, Field

from boba.agent.agent import Agent, AgentContext
from boba.agent.events import AgentEvent
from boba.agent.history import HistoryService, HistoryWriter, InMemoryHistoryService
from boba.agent.middleware import (
    AgentErrorRouter,
    AgentErrorRouterMiddleware,
    EventStamperMiddleware,
    HistoryRecorderMiddleware,
    IterationCounterConfig,
    IterationCounterMiddleware,
    LLMPort,
    StopIfContentFilter,
    StopIfLengthReached,
    StopIfReasonStop,
    StopOnAnyFailure,
    ToolExecutionMiddleware,
    UserQueryRecorderMiddleware,
)
from boba.agent.turn.builder import TurnBuilder
from boba.agent.turn.history_view import HistoryDialogView
from boba.llm.builder import LLM
from boba.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
    StreamSourceLoop,
)
from boba.settings import ConfigSource, TomlEnvConfigSource
from boba.tools import (
    DEFAULT_PLUGIN_GROUP,
    DuplicateProviderError,
    FromConfig,
    Scope,
    ToolDeclarationError,
    discover_plugins,
)
from boba.tools.adapter import DishkaTool
from boba.tools.decorators import (
    enable_if_predicate,
    has_enable_if,
    is_provider,
    is_tool,
    provider_scope,
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

_DEFAULT_COMPONENT: str = ""
"""Dishka default-component (app-level services)."""

_INLINE_COMPONENT: str = "inline"
"""Component для tools/providers, добавленных через `use_tools(...)`."""

_DEFAULT_MIDDLEWARES: tuple[type, ...] = (
    HistoryRecorderMiddleware,
    EventStamperMiddleware,
    AgentErrorRouterMiddleware,
    IterationCounterMiddleware,
    ToolExecutionMiddleware,
    UserQueryRecorderMiddleware,
)
"""Default chain от внешнего к внутреннему — порядок имеет значение."""


class AgentBuilderConfig(BaseModel):
    """DTO bootstrap-конфига AgentBuilder.

    Агрегирует per-middleware конфиги, нужные на этапе сборки агента.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    iteration_counter: IterationCounterConfig = Field(
        default_factory=IterationCounterConfig,
    )


@dataclass(frozen=True)
class _ProviderEntry:
    """Регистрационная единица: factory + scope + component + cached план вызова."""

    fn: Callable[..., Any]
    scope: Scope
    component: str
    plan: CallPlan


@dataclass(frozen=True)
class _ToolEntry:
    """Зарегистрированный `@tool` callable: объект + component + cached план."""

    obj: Any
    component: str
    plan: CallPlan


class AgentBuilder:
    """Fluent-фасад: собирает Agent через единый DI-резолвер.

    Owns: LLM, history journal, Dishka `Container` (через накопленные
    providers), bootstrap-конфиг middleware-цепочки, middleware list.
    Turn-side (model/prompts/sampling/reducers) живёт в `TurnBuilder` и
    подаётся одним методом `.use_turn(...)`.
    """

    def __init__(self) -> None:
        self._llm: LLM | None = None
        self._providers: list[_ProviderEntry] = []
        self._tools: list[_ToolEntry] = []
        self._discover_groups: list[str] = []
        self._history_service: HistoryService = InMemoryHistoryService()
        self._turn: TurnBuilder | None = None
        self._middlewares: list[type] = list(_DEFAULT_MIDDLEWARES)
        self._terminal_cls: type = LLMPort
        self._builder_config: AgentBuilderConfig = AgentBuilderConfig()
        # Единый источник конфигурации для всех FromConfig-загрузок.
        # Default — TOML из $BOBA_CONFIG_PATH + os.environ; для тестов или
        # custom-flow можно подменить через `use_config(...)`.
        self._config_source: ConfigSource = TomlEnvConfigSource()
        # Заполняется внутри build() перед сборкой chain — closure ToolExecutor
        # provider'а ищет здесь свежий registry.
        self._registry: ToolRegistry | None = None
        # Заполняется внутри `_register_internal_providers` — синглтон на сессию.
        self._error_router: AgentErrorRouter = AgentErrorRouter()

    # --- LLM / lifecycle --------------------------------------------------- #

    def with_llm(self, llm: LLM) -> Self:
        """Готовый LLM (обязательно; см. LLMBuilder)."""
        self._llm = llm
        return self

    def with_history(self, service: HistoryService) -> Self:
        """Журнал AgentEvent; дефолт — InMemoryHistoryService()."""
        self._history_service = service
        return self

    def use_turn(self, turn: TurnBuilder) -> Self:
        """Описание следующего хода. Обязательно до `.build()`."""
        self._turn = turn
        return self


    def use_config(self, source: ConfigSource) -> Self:
        """
        Переопределить ConfigSource
        """
        self._config_source = source
        return self

    def use_event_stamper(self, cls: type) -> Self:
        """
        Заменить класс EventStamper в middleware
        """
        try:
            idx = self._middlewares.index(EventStamperMiddleware)
        except ValueError:
            self._middlewares.append(cls)
        else:
            self._middlewares[idx] = cls
        return self

    # --- DI / Tools registration ------------------------------------------ #

    def register_provider(
        self,
        fn: Callable[..., Any],
        *,
        scope: Scope = Scope.APP,
        component: str = _DEFAULT_COMPONENT,
    ) -> Self:
        """
        Точка регистрации provider в DI
        """
        plan = introspect_callable(fn)
        self._validate_provider(fn, plan)
        for existing in self._providers:
            if (
                existing.component == component
                and existing.plan.return_type is plan.return_type
            ):
                msg = (
                    f"component {component!r}: тип {plan.return_type!r} "
                    f"уже зарегистрирован другим provider'ом — в одной "
                    f"component-зоне один provider на тип"
                )
                raise DuplicateProviderError(msg)
        self._providers.append(
            _ProviderEntry(fn=fn, scope=scope, component=component, plan=plan),
        )
        return self

    def use_tools(self, items: Iterable[Any]) -> Self:
        """Зарегистрировать tool"""
        for item in items:
            self._absorb_item(item, _INLINE_COMPONENT)
        return self

    def use_plugin(self, plugin_module: object) -> Self:
        """Подцепить v2-плагин — Python-модуль с `@tool`/`@provides`."""
        component = getattr(plugin_module, "__name__", repr(plugin_module))
        for attr_name in dir(plugin_module):
            if attr_name.startswith("_"):
                continue

            obj = getattr(plugin_module, attr_name, None)
            if obj is None:
                continue

            if is_tool(obj) or is_provider(obj):
                self._absorb_item(obj, component)

        return self

    def use_plugins(
        self,
        group: str = DEFAULT_PLUGIN_GROUP,
    ) -> Self:
        """Подцепить плагины через entry-points group."""
        self._discover_groups.append(group)
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

    def build(self) -> Agent:
        """Собрать Agent. Требуются `.with_llm(...)` и `.use_turn(...)`."""
        if self._llm is None:
            msg = "AgentBuilder.build: .with_llm(...) обязателен до .build()"
            raise ValueError(msg)
        if self._turn is None:
            msg = "AgentBuilder.build: .use_turn(...) обязателен до .build()"
            raise ValueError(msg)

        # 1. Discovery откладывалась до build'а.
        for group in self._discover_groups:
            for module in discover_plugins(group):
                self.use_plugin(module)
        self._discover_groups.clear()

        # 2. Регистрируем internal-провайдеры: LLM, History, ToolExecutor, etc.
        self._register_internal_providers()

        # 3. Резолвим FromConfig-инстансы (один раз) и отсеиваем entries
        #    с `enable_if`, вернувшим False. Делается ДО сборки контейнера —
        #    выключенные tools/providers вообще не попадают в DI.
        configs = self._instantiate_configs()
        self._apply_enable_if_filter(configs)

        # 4. Собираем Container из всех уцелевших providers + configs.
        container = self._build_container(configs)

        # 5. Строим ToolRegistry (DishkaTool'ы) — нужен Container, но НЕ
        #    нужен ToolExecutor (closure-провайдер найдёт self._registry).
        self._registry = self._build_registry(container)

        # 6. Auto-wire turn'а: history_view и tool_catalog подкладываем по
        #    умолчанию, если turn их не задал явно.
        if not self._turn.has_history_view():
            self._turn.with_history_view(HistoryDialogView(self._history_service))
        if not self._turn.has_tool_catalog():
            self._turn.with_tool_catalog(self._registry.catalog())

        # 7. Собираем middleware-chain через DI: каждый класс конструируется
        #    container.get(...) для своих non-inner параметров.
        chain = self._build_chain_via_di(container)
        source = StreamSourceLoop(
            source=chain,
            stop_if=(
                StopIfReasonStop()
                .or_(StopIfLengthReached())
                .or_(StopIfContentFilter())
                .or_(StopOnAnyFailure())
            ),
        )
        return Agent(source=source, container=container)

    # --- Internal helpers -------------------------------------------------- #

    def _absorb_item(self, obj: Any, component: str) -> None:
        """Маршрутизировать item (@tool / @provides) в внутренние pool'ы."""
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

    @staticmethod
    def _validate_provider(fn: Callable[..., Any], plan: CallPlan) -> None:
        """Provider обязан иметь return type annotation."""
        if plan.return_type is Parameter.empty or plan.return_type is None:
            msg = (
                f"@provides function {getattr(fn, '__name__', repr(fn))!r}: "
                f"return type обязателен — это тип, под которым служба "
                f"регистрируется в DI"
            )
            raise ToolDeclarationError(msg)

    # --- Internal providers (agent core services) ------------------------- #

    def _register_internal_providers(self) -> None:
        """Зарегистрировать в DI всё, что нужно middleware/agent'у.

        Это «склейка» между явными полями builder'а (with_llm/with_history)
        и DI-резолюцией middleware'ов. Каждое поле оборачивается в
        bound-метод с типизированным return — `register_provider`
        интроспектирует подпись без `self`.
        """
        internal_factories: tuple[Callable[..., Any], ...] = (
            self._provide_history_service,
            self._provide_history_writer,
            self._provide_llm,
            self._provide_turn,
            self._provide_agent_builder_config,
            self._provide_iteration_counter_config,
            self._provide_error_router,
            self._provide_tool_executor,
        )
        for fn in internal_factories:
            self.register_provider(fn, scope=Scope.APP)

    def _provide_history_service(self) -> HistoryService:
        return self._history_service

    def _provide_history_writer(self) -> HistoryWriter:
        return self._history_service

    def _provide_llm(self) -> LLM:
        if self._llm is None:
            msg = "_provide_llm called before with_llm() — invariant broken"
            raise RuntimeError(msg)
        return self._llm

    def _provide_turn(self) -> TurnBuilder:
        if self._turn is None:
            msg = "_provide_turn called before use_turn() — invariant broken"
            raise RuntimeError(msg)
        return self._turn

    def _provide_agent_builder_config(self) -> AgentBuilderConfig:
        return self._builder_config

    def _provide_iteration_counter_config(self) -> IterationCounterConfig:
        return self._builder_config.iteration_counter

    def _provide_error_router(self) -> AgentErrorRouter:
        return self._error_router

    def _provide_tool_executor(self) -> ToolExecutor:
        """Lazy: registry строится после регистрации provider'а, но ДО
        первого `container.get(ToolExecutor)` при сборке chain'а.
        На этот момент `self._registry` уже выставлен в `build()`.
        """
        if self._registry is None:
            msg = "ToolExecutor запрошен раньше, чем построен ToolRegistry"
            raise RuntimeError(msg)
        return self._registry.executor()

    # --- Container assembly ----------------------------------------------- #

    def _build_container(self, configs: dict[type, object]) -> Container:
        """Собрать Dishka Container из накопленных providers и configs.

        `configs` — pre-instantiated FromConfig-инстансы (см.
        `_instantiate_configs`). Передаются в `make_container(context=...)`
        и регистрируются `from_context()` в default-provider'е.
        """
        config_types = set(configs.keys())

        components: dict[str, list[_ProviderEntry]] = {}
        for p in self._providers:
            components.setdefault(p.component, []).append(p)
        for t in self._tools:
            components.setdefault(t.component, [])

        default_entries = components.pop(_DEFAULT_COMPONENT, [])
        default_provider = self._build_default_dishka_provider(
            default_entries,
            config_types,
        )
        plugin_providers = [
            self._build_plugin_dishka_provider(component_name, entries)
            for component_name, entries in components.items()
        ]
        return make_container(
            default_provider,
            *plugin_providers,
            context=configs,
        )

    # --- Config resolution + enable_if filter ----------------------------- #

    def _instantiate_configs(self) -> dict[type, object]:
        """Собрать ВСЕ FromConfig-типы (из tools, providers И из
        `enable_if`-предикатов) и инстанцировать каждый ровно один раз.

        Каждый cfg-тип загружается через `self._config_source.for_path(...)`,
        path извлекается из `cfg_type.model_config["boba_config_path"]`.
        Полученный dict проходит через `cfg_type.model_validate(...)` —
        pydantic-валидация + flat-redistribute, но БЕЗ внутренней
        source-машинерии `BaseSettings.__init__`. Это значит, что
        единственный читатель TOML/env — `ConfigSource`.

        Один инстанс одновременно используется для evaluate enable_if
        и для регистрации в Container — никогда не создаём конфиг дважды.
        """
        cfg_types: set[type] = set()

        for p in self._providers:
            for dep in p.plan.di_deps:
                if isinstance(dep.marker, FromConfig):
                    cfg_types.add(dep.target_type)
        for t in self._tools:
            for dep in t.plan.di_deps:
                if isinstance(dep.marker, FromConfig):
                    cfg_types.add(dep.target_type)

        # FromConfig-типы из предикатов enable_if — нужны ещё на стадии
        # фильтра, до Container'а. Если предикат использует FromDI — это
        # decl-ошибка, проверяется в _is_enabled.
        for target in self._enable_if_targets():
            predicate_plan = introspect_callable(enable_if_predicate(target))
            for dep in predicate_plan.di_deps:
                if isinstance(dep.marker, FromConfig):
                    cfg_types.add(dep.target_type)

        return {ct: self._load_config(ct) for ct in cfg_types}

    def _load_config(self, cfg_type: type) -> object:
        """Загрузить один cfg через ConfigSource → `model_validate`."""
        path = self._config_path_for(cfg_type)
        data = self._config_source.for_path(path)
        return cfg_type.model_validate(data)

    @staticmethod
    def _config_path_for(cfg_type: type) -> tuple[str, ...]:
        """Извлечь `ConfigPath` из `cfg_type.model_config.boba_config_path`.

        `boba_config_path` может быть строкой ("tool.files") или
        кортежем сегментов (("tool", "files")). Если не задан — путь
        пуст, ConfigSource вернёт пустой dict → cfg получит все default'ы.
        """
        mc = getattr(cfg_type, "model_config", {})
        section = mc.get("boba_config_path", "") if isinstance(mc, dict) else ""
        if isinstance(section, str):
            return tuple(s for s in section.split(".") if s)
        return tuple(section)

    def _apply_enable_if_filter(self, configs: dict[type, object]) -> None:
        """Удалить из накопленных entries те, чьи `enable_if` вернули False.

        Удаление идёт по `self._providers` и `self._tools` — выключенные
        не попадут ни в DI-Container, ни в ToolRegistry. Internal-providers
        (LLM, History, ToolExecutor, …) лишены `enable_if` и не отсеиваются.
        """
        self._providers = [
            p for p in self._providers if self._is_entry_enabled(p.fn, configs)
        ]
        self._tools = [t for t in self._tools if self._is_entry_enabled(t.obj, configs)]

    def _enable_if_targets(self) -> Iterable[Any]:
        """Все накопленные entries (provider-fn'ы + tool-callables)
        с пометкой `enable_if`. Используется в `_instantiate_configs`,
        чтобы знать какие FromConfig-типы нужно инстанцировать заранее.
        """
        for p in self._providers:
            if has_enable_if(p.fn):
                yield p.fn
        for t in self._tools:
            if has_enable_if(t.obj):
                yield t.obj

    @staticmethod
    def _is_entry_enabled(target: Any, configs: dict[type, object]) -> bool:
        """Запустить `enable_if`-predicate, если он есть.

        Predicate должен использовать только FromConfig-deps — на момент
        проверки DI-Container ещё не построен. FromDI в подписи predicate'а
        → `ToolDeclarationError`.
        """
        if not has_enable_if(target):
            return True
        predicate = enable_if_predicate(target)
        plan = introspect_callable(predicate)
        kwargs: dict[str, Any] = {}
        for dep in plan.di_deps:
            if not isinstance(dep.marker, FromConfig):
                pred_name = getattr(predicate, "__name__", repr(predicate))
                msg = (
                    f"enable_if {pred_name!r}: параметр {dep.param_name!r} "
                    f"использует {type(dep.marker).__name__} — в enable_if "
                    f"допустимы только FromConfig (DI ещё не существует на "
                    f"стадии фильтра)"
                )
                raise ToolDeclarationError(msg)
            kwargs[dep.param_name] = configs[dep.target_type]
        return bool(predicate(**kwargs))

    @staticmethod
    def _build_default_dishka_provider(
        entries: list[_ProviderEntry],
        config_types: set[type],
    ) -> Provider:
        provider = Provider(scope=to_dishka_scope(Scope.APP))
        for cfg_type in config_types:
            provider.from_context(
                provides=cfg_type,
                scope=to_dishka_scope(Scope.APP),
            )
        for entry in entries:
            provider.provide(
                source=entry.fn,
                scope=to_dishka_scope(entry.scope),
            )
        return provider

    def _build_plugin_dishka_provider(
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
            provider.provide(
                source=entry.fn,
                scope=to_dishka_scope(entry.scope),
            )
        return provider

    def _build_registry(self, container: Container) -> ToolRegistry:
        """Обернуть `@tool` callables в `DishkaTool` и собрать ToolSource'ы."""
        by_component: dict[str, list[_ToolEntry]] = {}
        for t in self._tools:
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

    # --- Chain assembly via DI -------------------------------------------- #

    def _build_chain_via_di(
        self,
        container: Container,
    ) -> StreamSource[AgentContext, AgentEvent]:
        """Construct middleware-цепочку, резолвя non-`inner` deps через DI."""
        chain_builder = StreamSourceChainBuilder[AgentContext, AgentEvent]()
        for middleware_cls in self._middlewares:
            # Замыкание над cls и container — каждый middleware конструируется
            # при `chain_builder.terminal(...)`-разворачивании.
            chain_builder.use(
                lambda inner, _cls=middleware_cls: self._make_middleware(
                    _cls,
                    inner,
                    container,
                ),
            )
        terminal = self._make_terminal(self._terminal_cls, container)
        return chain_builder.terminal(terminal)

    @staticmethod
    def _make_middleware(
        middleware_cls: type,
        inner: StreamSource[AgentContext, AgentEvent],
        container: Container,
    ) -> StreamSource[AgentContext, AgentEvent]:
        """Сконструировать middleware: inner позиционно, остальное — из DI."""
        kwargs = _resolve_init_kwargs(
            middleware_cls,
            container,
            exclude={"inner"},
        )
        return middleware_cls(inner, **kwargs)

    @staticmethod
    def _make_terminal(
        terminal_cls: type,
        container: Container,
    ) -> StreamSource[AgentContext, AgentEvent]:
        """Terminal middleware — нет inner, все params через DI."""
        kwargs = _resolve_init_kwargs(terminal_cls, container, exclude=set())
        return terminal_cls(**kwargs)


def _resolve_init_kwargs(
    cls: type,
    container: Container,
    *,
    exclude: set[str],
) -> dict[str, Any]:
    """`cls.__init__` → kwargs, резолвя каждый non-excluded param из DI.

    Конвенция: каждый параметр (кроме `self` и `exclude`) — типизирован,
    его тип используется как DI-ключ для `container.get(T)`. Annotated-
    маркеры не требуются: для middleware'ов «всё что в подписи — это DI».
    """
    sig = inspect.signature(cls.__init__)
    hints = get_type_hints(cls.__init__)
    kwargs: dict[str, Any] = {}
    skip = exclude | {"self"}
    for name, param in sig.parameters.items():
        if name in skip:
            continue
        annotation = hints.get(name, param.annotation)
        if annotation is Parameter.empty:
            msg = (
                f"{cls.__name__}: параметр {name!r} без аннотации; "
                f"middleware-конструктор должен типизировать все non-inner deps"
            )
            raise TypeError(msg)
        kwargs[name] = container.get(annotation)
    return kwargs


def _resolve_callable(obj: Any) -> Any:
    """`@tool`-class → instance (`obj()`), функция/instance — as-is."""
    if inspect.isclass(obj):
        return obj()
    return obj


_SID_INVALID_CHAR = re.compile(r"[^A-Za-z0-9_-]")


def _component_to_source_id(component_name: str) -> str:
    """Превратить имя компонента (модуля плагина) в валидный `ToolSourceId`.

    `ToolSourceId` ограничен `[A-Za-z0-9][A-Za-z0-9_-]*`, а discovered
    component обычно равен `module.__name__` (например `boba.tool.shell`)
    с точками. Заменяем всё не-валидное на `_`, обеспечивая стартовый
    alphanumeric.
    """
    sanitized = _SID_INVALID_CHAR.sub("_", component_name)
    if not sanitized or not sanitized[0].isalnum():
        sanitized = "p_" + sanitized
    return sanitized
