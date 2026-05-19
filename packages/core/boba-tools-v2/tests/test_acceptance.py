"""Acceptance-тесты `boba-tools-v2`.

Минимальный happy path: app-провайдер регистрирует `Greeter`, плагин
объявляет `@tool` его потребляющий через `FromDI`. Сборка → invoke →
правильный результат.

Дальше: plugin-овый `@provides`, component-override (плагин и app оба
предоставляют один тип), функция-tool (вместо классов), валидация
LLM-args через pydantic.
"""

from __future__ import annotations

import types
from typing import Annotated

import pytest

from boba.tools_v2 import (
    AgentBuilder,
    DuplicateProviderError,
    FromDI,
    Scope,
    ToolDeclarationError,
    provides,
    tool,
)
from boba.tools.domain.ids import ToolSourceId
from boba.tools.domain.result import JsonResult, TextResult, ToolResult
from boba.tools.domain.tool import ToolCall, ToolContext


# --------------------------------------------------------------------------- #
# Test fixtures: simple service классы для DI                                 #
# --------------------------------------------------------------------------- #

class Greeter:
    """Простой сервис: возвращает приветствие."""

    def __init__(self, suffix: str = "!") -> None:
        self._suffix = suffix

    def greet(self, name: str) -> str:
        return f"Hello, {name}{self._suffix}"


class Counter:
    """Stateful сервис: счётчик вызовов."""

    def __init__(self) -> None:
        self.count = 0

    def tick(self) -> int:
        self.count += 1
        return self.count


class HelloCfg:
    """Имитация Pydantic-settings: класс с пустым __init__, читает env/dflt."""

    def __init__(self) -> None:
        self.suffix = "!!!"


# --------------------------------------------------------------------------- #
# Helpers: собрать plugin как ad-hoc модуль                                   #
# --------------------------------------------------------------------------- #

def _make_plugin_module(name: str, **attrs: object) -> types.ModuleType:
    """Собрать виртуальный модуль с заданными атрибутами для add_plugin."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _invoke(registry, source_id: str, tool_name: str, args: dict) -> ToolResult:
    """Достать tool из registry и позвать через executor."""
    executor = registry.executor()
    full_id = f"{source_id}__{tool_name}"
    return executor.execute(
        ToolContext(),
        ToolCall(tool_id=full_id, arguments=args),
    )


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #

def test_app_provider_plus_plugin_tool_via_fromdi() -> None:
    """App регистрирует Greeter → плагин потребляет через FromDI."""

    def greeter_factory() -> Greeter:
        return Greeter("!")

    @tool
    class HelloTool:
        """Say hello to someone."""

        def __call__(
            self,
            name: Annotated[str, "Person to greet"],
            greeter: Annotated[Greeter, FromDI(Scope.APP)],
        ) -> str:
            return greeter.greet(name)

    plugin = _make_plugin_module("plugin_demo", HelloTool=HelloTool)
    registry = (
        AgentBuilder()
        .register(greeter_factory, scope=Scope.APP)
        .add_plugin(plugin)
        .build()
    )

    result = _invoke(registry, "plugin_demo", "hello", {"name": "World"})

    assert isinstance(result, TextResult)
    assert result.text == "Hello, World!"


def test_plugin_provider_with_fromconfig() -> None:
    """Плагин сам провайдит сервис, получая cfg через FromConfig."""

    @provides(scope=Scope.APP)
    def greeter(cfg: Annotated[HelloCfg, FromDI(Scope.APP)]) -> Greeter:
        return Greeter(cfg.suffix)

    @tool
    class HelloTool:
        """Say hello."""

        def __call__(
            self,
            name: Annotated[str, "Who to greet"],
            greeter: Annotated[Greeter, FromDI(Scope.APP)],
        ) -> str:
            return greeter.greet(name)

    # Регистрируем cfg в app — это типичный паттерн: cfg-инстанс
    # инжектится через app-level register.
    def cfg_factory() -> HelloCfg:
        return HelloCfg()

    plugin = _make_plugin_module(
        "plugin_demo2",
        greeter=greeter,
        HelloTool=HelloTool,
    )
    registry = (
        AgentBuilder()
        .register(cfg_factory, scope=Scope.APP)
        .add_plugin(plugin)
        .build()
    )

    result = _invoke(registry, "plugin_demo2", "hello", {"name": "World"})
    assert isinstance(result, TextResult)
    assert result.text == "Hello, World!!!"


def test_plugin_function_tool_returning_dict() -> None:
    """Функция-tool (не класс), возвращает dict → JsonResult."""

    def counter_factory() -> Counter:
        return Counter()

    @tool
    def tick(counter: Annotated[Counter, FromDI(Scope.APP)]) -> dict:
        """Tick the counter."""
        return {"value": counter.tick()}

    plugin = _make_plugin_module("plugin_demo3", tick=tick)
    registry = (
        AgentBuilder()
        .register(counter_factory, scope=Scope.APP)
        .add_plugin(plugin)
        .build()
    )

    result1 = _invoke(registry, "plugin_demo3", "tick", {})
    result2 = _invoke(registry, "plugin_demo3", "tick", {})
    assert isinstance(result1, JsonResult)
    assert isinstance(result2, JsonResult)
    # APP-scope: один и тот же counter — счёт растёт между вызовами.
    assert result1.payload == {"value": 1}
    assert result2.payload == {"value": 2}


def test_component_override_plugin_wins_for_its_tools() -> None:
    """App и plugin оба провайдят Greeter — plugin'овые tools видят свой."""

    def app_greeter() -> Greeter:
        return Greeter(" (from app)")

    @provides(scope=Scope.APP)
    def plugin_greeter() -> Greeter:
        return Greeter(" (from plugin)")

    @tool
    class HelloTool:
        """Say hello using greeter."""

        def __call__(
            self,
            name: Annotated[str, "Name"],
            greeter: Annotated[Greeter, FromDI(Scope.APP)],
        ) -> str:
            return greeter.greet(name)

    plugin = _make_plugin_module(
        "plugin_override",
        plugin_greeter=plugin_greeter,
        HelloTool=HelloTool,
    )
    registry = (
        AgentBuilder()
        .register(app_greeter, scope=Scope.APP)
        .add_plugin(plugin)
        .build()
    )

    result = _invoke(registry, "plugin_override", "hello", {"name": "X"})
    assert isinstance(result, TextResult)
    assert result.text == "Hello, X (from plugin)"


def test_tool_definition_emits_llm_schema() -> None:
    """`tool.definition()` отдаёт ToolSchema с pydantic-сгенерированной JsonSchema."""

    def greeter_factory() -> Greeter:
        return Greeter("!")

    @tool
    class HelloTool:
        """Say hello."""

        def __call__(
            self,
            name: Annotated[str, "Person to greet"],
            greeter: Annotated[Greeter, FromDI(Scope.APP)],
        ) -> str:
            return greeter.greet(name)

    plugin = _make_plugin_module("plugin_schema", HelloTool=HelloTool)
    registry = (
        AgentBuilder()
        .register(greeter_factory, scope=Scope.APP)
        .add_plugin(plugin)
        .build()
    )

    catalog = registry.catalog()
    defs = list(catalog.definitions())
    assert len(defs) == 1
    schema = defs[0]
    assert schema.name == "plugin_schema__hello"
    # `greeter` (FromDI) НЕ должен попасть в JsonSchema
    props = schema.parameters_schema.get("properties", {})
    assert "name" in props
    assert "greeter" not in props


def test_duplicate_app_provider_raises() -> None:
    """Два app-провайдера на один тип — ошибка."""

    def g1() -> Greeter:
        return Greeter("1")

    def g2() -> Greeter:
        return Greeter("2")

    builder = AgentBuilder().register(g1, scope=Scope.APP)
    with pytest.raises(DuplicateProviderError):
        builder.register(g2, scope=Scope.APP)


def test_duplicate_plugin_provider_raises() -> None:
    """Два provider'а на один тип в одном плагине — ошибка."""

    @provides(scope=Scope.APP)
    def a() -> Greeter:
        return Greeter("a")

    @provides(scope=Scope.APP)
    def b() -> Greeter:
        return Greeter("b")

    plugin = _make_plugin_module("plugin_dup", a=a, b=b)
    with pytest.raises(DuplicateProviderError):
        AgentBuilder().add_plugin(plugin).build()


def test_provider_without_return_type_raises() -> None:
    """`@provides` без return type — ошибка декларации."""

    @provides(scope=Scope.APP)
    def no_return():  # noqa: ANN202 — намеренно нет return-аннотации
        return Greeter("x")

    plugin = _make_plugin_module("plugin_bad", no_return=no_return)
    with pytest.raises(ToolDeclarationError):
        AgentBuilder().add_plugin(plugin).build()


def test_tool_without_annotation_raises() -> None:
    """Параметр tool'а без типа — ошибка декларации."""

    @tool
    class BadTool:
        """Bad: param without annotation."""

        def __call__(self, name) -> str:  # noqa: ANN001 — намеренно нет аннотации
            return f"hi {name}"

    plugin = _make_plugin_module("plugin_bad2", BadTool=BadTool)
    with pytest.raises(ToolDeclarationError):
        AgentBuilder().add_plugin(plugin).build()


def test_request_scope_creates_new_instance_per_invoke() -> None:
    """REQUEST-scope: каждый invoke получает новый Counter."""

    @provides(scope=Scope.REQUEST)
    def fresh_counter() -> Counter:
        return Counter()

    @tool
    def tick(counter: Annotated[Counter, FromDI(Scope.REQUEST)]) -> dict:
        """Tick request-scoped counter."""
        return {"value": counter.tick()}

    plugin = _make_plugin_module(
        "plugin_req",
        fresh_counter=fresh_counter,
        tick=tick,
    )
    registry = AgentBuilder().add_plugin(plugin).build()

    result1 = _invoke(registry, "plugin_req", "tick", {})
    result2 = _invoke(registry, "plugin_req", "tick", {})
    # REQUEST scope: каждый invoke — свежий Counter, всегда tick=1.
    assert isinstance(result1, JsonResult)
    assert isinstance(result2, JsonResult)
    assert result1.payload == {"value": 1}
    assert result2.payload == {"value": 1}


def test_unused_source_id_param() -> None:
    """ToolSourceId генерируется из имени модуля плагина."""

    def g() -> Greeter:
        return Greeter("!")

    @tool
    def greet(
        name: Annotated[str, "Name"],
        greeter: Annotated[Greeter, FromDI(Scope.APP)],
    ) -> str:
        """Greet."""
        return greeter.greet(name)

    plugin = _make_plugin_module("my_custom_plugin", greet=greet)
    registry = (
        AgentBuilder()
        .register(g, scope=Scope.APP)
        .add_plugin(plugin)
        .build()
    )

    # ToolSourceId должен совпадать с именем модуля.
    sources = registry.sources
    assert ToolSourceId("my_custom_plugin") in sources
