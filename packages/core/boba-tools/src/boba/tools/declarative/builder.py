"""ToolBuilder — composition root tool layer'а.

Аккумулирует две вещи:
- DI-провайдеры (@provides-фабрики + готовые инстансы) -> Dishka Container
  для резолва FromDI/FromConfig-параметров в tool'ах.
- @tool-callables -> ToolRegistry, в котором они доступны LLM.

build() отдаёт ToolRegistry, владеющий контейнером (закрывает его на
close()). Плагины различаются по origin (имя модуля), которое
становится ToolSourceId — это даёт LLM-namespace <plugin>__<tool>.
Container — плоский, без component-разделения: коллизии типов между
плагинами падают явным DuplicateProviderError в register_*.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import Enum, auto
from typing import Annotated, Any, Self, cast

from dishka import Container, Provider, make_container

from boba.tools.declarative.adapter import DishkaTool
from boba.tools.declarative.config import (
    ConfigResolver,
    PluginFilterAllowAll,
    PluginToolFilter,
)
from boba.tools.declarative.decorators import (
    is_provider,
    is_tool,
    provider_scope,
    tool_name,
)
from boba.tools.declarative.errors import (
    DuplicateProviderError,
    ToolDeclarationError,
    UnresolvedDependencyError,
)
from boba.tools.declarative.inject import FromConfig, FromDI
from boba.tools.declarative.introspect import CallPlan, build_call_plan
from boba.tools.declarative.scope import Scope, to_dishka_scope
from boba.tools.domain.ids import ToolName, sanitize_source_id
from boba.tools.framework.registry import (
    StaticToolSource,
    ToolRegistry,
    ToolSource,
)

__all__ = ["ToolBuilder"]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderEntry:
    fn: Callable[..., Any]
    scope: Scope
    plan: CallPlan
    plugin: str = ""

    @property
    def label(self) -> str:
        """Имя для диагностики; provider идентифицируется return-типом."""
        return getattr(self.fn, "__name__", repr(self.fn))


@dataclass(frozen=True)
class ToolEntry:
    obj: Any
    origin: str
    plan: CallPlan
    name: str
    plugin: str = ""

    @property
    def label(self) -> str:
        """Имя для диагностики; совпадает с wire-именем tool'а."""
        return self.name


class _DeclKind(Enum):
    """Чем помечен объект декоратором."""

    TOOL = auto()
    PROVIDER = auto()


class _Declared:
    """Объект, классифицированный по декоратору единожды."""

    def __init__(self, obj: Any, kind: _DeclKind) -> None:
        self._obj = obj
        self._kind = kind

    @property
    def obj(self) -> Any:
        return self._obj

    @property
    def kind(self) -> _DeclKind:
        return self._kind


class _ModuleScanner:
    """Инкапсулирует обход плагин-модуля: origin + выборка @tool/@provides."""

    def __init__(self, module: object) -> None:
        self._module = module

    @property
    def origin(self) -> str:
        """Имя модуля -> origin tool'ов (становится ToolSourceId)."""
        return getattr(self._module, "__name__", repr(self._module))

    def iter_registrable(
        self,
        plugin_name: str,
        plugin_tool_filter: PluginToolFilter,
    ) -> Iterator[_Declared]:
        """Классифицированные объекты модуля, прошедшие фильтр."""
        for obj in self._iter_public_objects():
            if is_tool(obj):
                if not plugin_tool_filter.check_tool(plugin_name, tool_name(obj)):
                    continue

                yield _Declared(obj, _DeclKind.TOOL)

            if is_provider(obj):
                yield _Declared(obj, _DeclKind.PROVIDER)

    def _iter_public_objects(self) -> Iterator[Any]:
        """Публичные (не начинающиеся с '_') атрибуты модуля, кроме None."""
        for attr_name in dir(self._module):
            if attr_name.startswith("_"):
                continue

            obj = getattr(self._module, attr_name, None)
            if obj is None:
                continue

            yield obj


class ToolBuilder:
    """Fluent-фасад tool-слоя: providers + tools + plugins -> ToolRegistry."""

    def __init__(self) -> None:
        self._providers: list[ProviderEntry] = []
        self._tools: list[ToolEntry] = []

    def register_provider(
        self,
        fn: Callable[..., Any],
        *,
        scope: Scope = Scope.APP,
        plugin: str = "",
    ) -> Self:
        """
        Зарегистрировать DI-фабрику.

        Return type функции = провайдимый тип
        """
        plan = build_call_plan(fn)
        if plan.lacks_return_type():
            msg = (
                f"@provides function {getattr(fn, '__name__', repr(fn))!r}: "
                f"return type обязателен — это тип, под которым служба "
                f"регистрируется в DI"
            )
            raise ToolDeclarationError(msg)
        self._raise_if_taken(plan.return_type)
        self._providers.append(
            ProviderEntry(fn=fn, scope=scope, plan=plan, plugin=plugin)
        )
        return self

    def register_instance(
        self,
        instance: Any,
        *,
        provides: type | None = None,
        scope: Scope = Scope.APP,
    ) -> Self:
        """Зарегистрировать готовый инстанс под provides (или type(instance))."""
        target = provides if provides is not None else type(instance)

        def _factory() -> Any:
            return instance

        _factory.__annotations__ = {"return": target}
        _factory.__name__ = f"_provide_{target.__name__}"
        return self.register_provider(_factory, scope=scope)

    def use_config_resolver(self, resolver: ConfigResolver) -> Self:
        """
        Зарегистрировать ConfigResolver как обычный APP-scope provider.
        """
        return self.register_instance(resolver, provides=ConfigResolver)

    def use_tools(self, items: Iterable[Any]) -> Self:
        """
        Добавить tool'ы / провайдеры напрямую (без плагин-модуля)
        """
        for item in items:
            self._register(_Declared(item, _DeclKind.TOOL), "inline", "")

        return self

    def use_plugin(self, module: object) -> Self:
        """
        Подцепить плагин-модуль; все его @tool/@provides регистрируются
        """
        plugin_name = getattr(module, "__name__", repr(module))
        self._register_module(module, plugin_name, PluginFilterAllowAll())
        return self

    def discover_plugins(
        self,
        entry_point: str,
        plugin_tool_filter: PluginToolFilter,
    ) -> Self:
        """
        Загрузить плагины из entry-points, отфильтровав их через PluginGate
        """
        for ep in importlib.metadata.entry_points(group=entry_point):
            if not plugin_tool_filter.check_plugin_name(ep.name):
                _logger.debug("plugin %r is not admitted by gate; skipped", ep.name)
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

            self._register_module(module, ep.name, plugin_tool_filter)

        return self

    @property
    def tools(self) -> list[ToolEntry]:
        """Read-only снимок зарегистрированных tool'ов."""
        return list(self._tools)

    @property
    def providers(self) -> list[ProviderEntry]:
        """Read-only снимок зарегистрированных provider-фабрик"""
        return list(self._providers)

    def build(self) -> ToolRegistry:
        """
        Собрать Dishka container и ToolRegistry поверх него.
        """
        self._validate_from_di()
        self._validate_from_config()

        di = make_container(self._build_provider())

        return ToolRegistry(
            sources=self._build_sources(di),
            container=di,
        )

    def _register_module(
        self,
        module: object,
        plugin_name: str,
        plugin_tool_filter: PluginToolFilter,
    ) -> None:
        """Просканировать модуль и зарегистрировать отобранные объекты."""
        scanner = _ModuleScanner(module)
        for decl in scanner.iter_registrable(plugin_name, plugin_tool_filter):
            self._register(decl, scanner.origin, plugin_name)

    def _register(self, decl: _Declared, origin: str, plugin: str = "") -> None:
        """Регистрирует уже классифицированный объект как tool или provider."""
        if decl.kind is _DeclKind.PROVIDER:
            self.register_provider(
                decl.obj, scope=provider_scope(decl.obj), plugin=plugin
            )

        elif decl.kind is _DeclKind.TOOL:
            plan = build_call_plan(decl.obj)
            self._tools.append(
                ToolEntry(
                    obj=decl.obj,
                    origin=origin,
                    plan=plan,
                    name=tool_name(decl.obj),
                    plugin=plugin,
                ),
            )

        else:
            raise ToolDeclarationError(
                f"ToolBuilder: {decl.obj!r} — не помечен ни как @tool, "
                f"ни как @provides; нечего регистрировать"
            )

    def _raise_if_taken(self, target: type) -> None:
        for p in self._providers:
            if p.plan.return_type is target:
                msg = f"тип {target!r} уже зарегистрирован provider'ом"
                raise DuplicateProviderError(msg)

    def _validate_from_config(self) -> None:
        """
        Авто-зарегистрировать provider'ы для всех FromConfig-типов
        """
        cfg_types = self._collect_config_types()
        if not cfg_types:
            return

        provided = {p.plan.return_type for p in self._providers}
        if ConfigResolver not in provided:
            names = ", ".join(sorted(t.__name__ for t in cfg_types))
            msg = (
                f"tool'ы/провайдеры объявляют FromConfig ({names}), но "
                f"ConfigResolver не зарегистрирован — вызови "
                f"use_config_resolver(...)"
            )
            raise UnresolvedDependencyError(msg)

        for cfg_type, plugin in cfg_types.items():
            if cfg_type not in provided:
                self.register_provider(
                    self._config_provider_factory(cfg_type, plugin)
                )

    def _collect_config_types(self) -> dict[type, str]:
        """Собрать FromConfig-типы с их плагином (секция = tool.<plugin>).

        Тип резолвится из секции плагина, в котором объявлен tool/provider.
        Один тип обычно принадлежит одному плагину; при коллизии остаётся первый.
        """
        cfg_types: dict[type, str] = {}
        for entry in (*self._providers, *self._tools):
            for dep in entry.plan.di_deps:
                if isinstance(dep.marker, FromConfig):
                    cfg_types.setdefault(dep.target_type, entry.plugin)
        return cfg_types

    def _validate_from_di(self) -> None:
        """
        Проверить, что каждый FromDI-тип имеет provider
        """
        provided = {p.plan.return_type for p in self._providers}

        missing: list[str] = []
        for entry in (*self._providers, *self._tools):
            for dep in entry.plan.di_deps:
                if isinstance(dep.marker, FromDI) and dep.target_type not in provided:
                    missing.append(f"{entry.label} -> {dep.target_type.__name__}")

        if missing:
            joined = ", ".join(sorted(set(missing)))
            msg = (
                f"FromDI-зависимости без зарегистрированного provider'а: "
                f"{joined}. Зарегистрируй provider "
                f"(register_provider/register_instance/@provides)"
            )
            raise UnresolvedDependencyError(msg)

    def _build_provider(self) -> Provider:
        provider = Provider(scope=to_dishka_scope(Scope.APP))

        for entry in self._providers:
            provider.provide(source=entry.fn, scope=to_dishka_scope(entry.scope))

        return provider

    def _build_sources(self, container: Container) -> list[ToolSource]:
        by_origin: dict[str, list[ToolEntry]] = {}

        for t in self._tools:
            by_origin.setdefault(t.origin, []).append(t)

        sources: list[ToolSource] = []
        for origin, tool_entries in by_origin.items():
            sid = sanitize_source_id(origin)
            dishka_tools = [
                DishkaTool(
                    target=self._callable_resolve(t.obj),
                    plan=t.plan,
                    container=container,
                    source_id=sid,
                    name=ToolName(t.name),
                )
                for t in tool_entries
            ]
            sources.append(StaticToolSource(sid, dishka_tools))

        return sources

    @staticmethod
    def _config_provider_factory(
        cfg_type: type,
        plugin: str,
    ) -> Callable[..., Any]:
        """Фабрика provider'а конфига: берёт ConfigResolver из DI -> resolve.

        Возвращает @provides-совместимый callable с подписью
        (resolver: Annotated[ConfigResolver, FromDI(APP)]) -> cfg_type, чтобы
        Dishka инжектил resolver и кешировал конфиг как APP-singleton. Секцию
        задаёт plugin (резолвится из tool.<plugin>).
        """

        def _factory(resolver: Any) -> Any:
            return resolver.resolve(cfg_type, plugin)

        _factory.__annotations__ = {
            "resolver": Annotated[ConfigResolver, FromDI(Scope.APP)],
            "return": cfg_type,
        }
        _factory.__name__ = f"_provide_config_{cfg_type.__name__}"
        return _factory

    @staticmethod
    def _callable_resolve(obj: Any) -> Callable[..., Any]:
        # класс здесь — фабрика: вызываемым становится его экземпляр (__call__)
        if inspect.isclass(obj):
            return cast("Callable[..., Any]", obj())

        return obj
