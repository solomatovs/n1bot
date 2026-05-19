"""
AgentBuilder + сборка Dishka-контейнера по plug-and-play модели.

Два phase:

1. **App phase** — `builder.register(fn, scope=...)`. Явные factories,
   которые app считает «коммунальными» (HttpClient, DbPool, etc.).
   Регистрируются в **default component** (`""`) Dishka.

2. **Plugin phase** — `builder.add_plugin(module)`. Framework сам
   обходит модуль, ищет `@tool` и `@provides` объекты, регистрирует
   их в **отдельном component** (имя = `module.__name__`).

`.build()` собирает:
- все `FromConfig`-типы из подписей → auto-load Pydantic-settings →
  context Dishka-контейнера, регистрируются в default через
  `from_context(...)`;
- все `@provides` функции → `Provider.provide(source=fn, scope=...)`;
- aliases плагинских component'ов на default для типов, которые
  плагин использует, но сам не провайдит (без alias Dishka требует
  явный `FromComponent('')` в подписи);
- все `@tool` функции → оборачиваются в `DishkaTool` (см. `adapter.py`)
  и попадают в `ToolRegistry` как `StaticToolSource` per-plugin.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Self

from dishka import Container, Provider, make_container
from dishka.entities.component import Component

from boba.tools.domain.ids import ToolSourceId
from boba.tools.framework.registry import (
    StaticToolSource,
    ToolRegistry,
)
from boba.tools_v2.decorators import (
    is_provider,
    is_tool,
    provider_scope,
)
from boba.tools_v2.errors import (
    DuplicateProviderError,
    ToolDeclarationError,
)
from boba.tools_v2.introspect import CallPlan, introspect_callable
from boba.tools_v2.markers import FromConfig
from boba.tools_v2.scope import Scope, to_dishka_scope

__all__ = ["AgentBuilder"]


@dataclass(frozen=True)
class _ProviderEntry:
    """Регистрационная единица: factory function + scope + её план вызова."""

    fn: Callable[..., Any]
    scope: Scope
    plan: CallPlan


@dataclass(frozen=True)
class _PluginEntry:
    """Раскрытый plugin-модуль: его tools и providers."""

    module: ModuleType | object
    component_name: str
    tools: tuple[tuple[Any, CallPlan], ...]
    providers: tuple[_ProviderEntry, ...]


class AgentBuilder:
    """Builder для собирания `ToolRegistry` через Dishka-контейнер.

    Использование:

        builder = AgentBuilder()
        builder.register(http_factory, scope=Scope.APP)
        builder.add_plugin(my_plugin_module)
        registry = builder.build()
    """

    def __init__(self) -> None:
        self._app_providers: list[_ProviderEntry] = []
        self._plugins: list[ModuleType | object] = []

    def register(
        self, fn: Callable[..., Any], *, scope: Scope,
    ) -> Self:
        """Phase 1: зарегистрировать app-level factory в DI.

        Тип, под которым служба регистрируется — return annotation `fn`.
        Параметры `fn` — её DI-зависимости (FromDI/FromConfig).
        """
        plan = introspect_callable(fn)
        self._validate_provider(fn, plan)
        if self._app_provided_type_exists(plan.return_type):
            msg = (
                f"app phase: тип {plan.return_type!r} уже зарегистрирован "
                f"другим provider'ом — в одной зоне один provider на тип"
            )
            raise DuplicateProviderError(msg)
        self._app_providers.append(
            _ProviderEntry(fn=fn, scope=scope, plan=plan),
        )
        return self

    def add_plugin(self, plugin: ModuleType | object) -> Self:
        """Phase 2: добавить plugin-модуль (или объект с атрибутами).

        Framework на `.build()` обойдёт атрибуты, найдёт всё помеченное
        `@tool` и `@provides`. Имя component'а = `getattr(plugin, '__name__', repr)`.
        """
        self._plugins.append(plugin)
        return self

    def build(self) -> ToolRegistry:
        """Собрать DI-контейнер + ToolRegistry. Идемпотентно недетерминистическое
        (config-load может зависеть от env), но для одних и тех же inputs — same.
        """
        plugins = tuple(self._collect_plugins())
        self._validate_plugin_providers(plugins)

        config_types = self._collect_config_types(self._app_providers, plugins)
        contexts: dict[type, object] = {ct: ct() for ct in config_types}

        app_provider = self._build_app_provider(
            self._app_providers, config_types,
        )
        plugin_providers = [
            self._build_plugin_provider(pi) for pi in plugins
        ]
        container = make_container(
            app_provider, *plugin_providers, context=contexts,
        )
        return self._assemble_registry(container, plugins)

    # --- Phase 1 helpers --- #

    def _app_provided_type_exists(self, target_type: type) -> bool:
        return any(p.plan.return_type is target_type for p in self._app_providers)

    def _app_provided_types(self) -> set[type]:
        return {p.plan.return_type for p in self._app_providers}

    # --- Phase 2 helpers --- #

    def _collect_plugins(self) -> tuple[_PluginEntry, ...]:
        out: list[_PluginEntry] = []
        for plugin in self._plugins:
            tools_list: list[tuple[Any, CallPlan]] = []
            providers_list: list[_ProviderEntry] = []
            for attr_name in dir(plugin):
                if attr_name.startswith("_"):
                    continue
                obj = getattr(plugin, attr_name, None)
                if obj is None:
                    continue
                if is_tool(obj):
                    plan = introspect_callable(obj)
                    tools_list.append((obj, plan))
                elif is_provider(obj):
                    scope = provider_scope(obj)
                    plan = introspect_callable(obj)
                    self._validate_provider(obj, plan)
                    providers_list.append(
                        _ProviderEntry(fn=obj, scope=scope, plan=plan),
                    )
            component_name = getattr(plugin, "__name__", repr(plugin))
            out.append(
                _PluginEntry(
                    module=plugin,
                    component_name=component_name,
                    tools=tuple(tools_list),
                    providers=tuple(providers_list),
                ),
            )
        return tuple(out)

    def _validate_plugin_providers(
        self, plugins: tuple[_PluginEntry, ...],
    ) -> None:
        """Внутри одного плагина — один provider на тип."""
        for pi in plugins:
            seen: set[type] = set()
            for p in pi.providers:
                if p.plan.return_type in seen:
                    msg = (
                        f"plugin {pi.component_name!r}: тип "
                        f"{p.plan.return_type!r} зарегистрирован дважды"
                    )
                    raise DuplicateProviderError(msg)
                seen.add(p.plan.return_type)

    @staticmethod
    def _validate_provider(fn: Callable[..., Any], plan: CallPlan) -> None:
        """Provider обязан иметь return type annotation."""
        from inspect import Parameter  # noqa: PLC0415

        if plan.return_type is Parameter.empty or plan.return_type is None:
            msg = (
                f"@provides function {getattr(fn, '__name__', repr(fn))!r}: "
                f"return type обязателен — это тип, под которым служба "
                f"регистрируется в DI"
            )
            raise ToolDeclarationError(msg)

    # --- Config collection --- #

    @staticmethod
    def _collect_config_types(
        app_providers: list[_ProviderEntry],
        plugins: tuple[_PluginEntry, ...],
    ) -> set[type]:
        out: set[type] = set()
        for p in app_providers:
            for dep in p.plan.di_deps:
                if isinstance(dep.marker, FromConfig):
                    out.add(dep.target_type)
        for pi in plugins:
            for _, plan in pi.tools:
                for dep in plan.di_deps:
                    if isinstance(dep.marker, FromConfig):
                        out.add(dep.target_type)
            for p in pi.providers:
                for dep in p.plan.di_deps:
                    if isinstance(dep.marker, FromConfig):
                        out.add(dep.target_type)
        return out

    # --- Provider assembly --- #

    @staticmethod
    def _build_app_provider(
        providers: list[_ProviderEntry],
        config_types: set[type],
    ) -> Provider:
        """`Provider` для default component'а: from_context для конфигов + provide для функций."""
        p = Provider(scope=to_dishka_scope(Scope.APP))
        for cfg_type in config_types:
            p.from_context(
                provides=cfg_type,
                scope=to_dishka_scope(Scope.APP),
            )
        for entry in providers:
            p.provide(source=entry.fn, scope=to_dishka_scope(entry.scope))
        return p

    @staticmethod
    def _build_plugin_provider(pi: _PluginEntry) -> Provider:
        """`Provider` для plugin-component'а.

        - `@provides` функции плагина → `provide(source=fn, scope=...)`.
        - Типы, которые tools или providers плагина требуют, но плагин не
          провайдит сам → `alias(source=T, component='')` в default.
          Без alias'а Dishka заставила бы tool автора писать
          `Annotated[T, FromComponent('')]`, что засоряет API.
        """
        local_types = {p.plan.return_type for p in pi.providers}

        # Все типы DI-deps из tools + providers
        used_types: set[type] = set()
        for _, plan in pi.tools:
            for dep in plan.di_deps:
                used_types.add(dep.target_type)
        for p in pi.providers:
            for dep in p.plan.di_deps:
                used_types.add(dep.target_type)

        # Те, что не локально провайдятся → надо аль'аснуть в default
        alias_types = used_types - local_types

        provider = Provider(
            scope=to_dishka_scope(Scope.APP),
            component=Component(pi.component_name),
        )
        for ct in alias_types:
            provider.alias(source=ct, component=Component(""))
        for entry in pi.providers:
            provider.provide(
                source=entry.fn, scope=to_dishka_scope(entry.scope),
            )
        return provider

    # --- ToolRegistry assembly --- #

    @staticmethod
    def _assemble_registry(
        container: Container,
        plugins: tuple[_PluginEntry, ...],
    ) -> ToolRegistry:
        """Обернуть plugin tools в `DishkaTool` и сложить в `StaticToolSource`'ы."""
        # Локальный импорт чтобы adapter.py мог импортировать ToolSource из
        # framework без циркуляции с container.py.
        from boba.tools_v2.adapter import (  # noqa: PLC0415
            DishkaTool,
        )

        sources: list[StaticToolSource] = []
        for pi in plugins:
            sid = ToolSourceId(pi.component_name)
            tools = [
                DishkaTool(
                    target=_resolve_callable(obj),
                    plan=plan,
                    container=container,
                    component=pi.component_name,
                    source_id=sid,
                )
                for obj, plan in pi.tools
            ]
            sources.append(StaticToolSource(sid, tools))
        return ToolRegistry.from_sources(sources)


def _resolve_callable(obj: Any) -> Any:
    """Если `@tool` навешен на класс — инстанциируем (`obj()`), иначе как есть.

    Stateless классы-tools `@tool class MyTool: def __call__(self, ...)` —
    инстанс создаётся один раз, шарится. Функции — используем напрямую.
    """
    import inspect  # noqa: PLC0415

    if inspect.isclass(obj):
        return obj()
    return obj
