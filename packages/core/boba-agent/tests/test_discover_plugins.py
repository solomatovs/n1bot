"""Unit-тесты `AgentBuilder.discover_plugins` — config-gated discovery.

Поведение, которое проверяем:
  - `enable=False` (или отсутствует) → плагин полностью пропущен,
    `ep.load()` не вызывается, ни tools, ни providers не регистрируются;
  - `enable=True` без `tools` → все `@tool` + `@provides` регистрируются;
  - `enable=True` + `tools=[name1, ...]` (allowlist) → только указанные
    `@tool`-функции; `@provides` регистрируются всегда;
  - источник конфига — переданный через `use_config(DictConfigSource(...))`.

Entry-points мокаются: `importlib.metadata.entry_points` через
monkeypatch возвращает кастомные fake-EntryPoint'ы, чьи `load()` отдают
in-memory тестовые модули.
"""

from __future__ import annotations

import types
from collections.abc import Iterable
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from boba.agent.builder import AgentBuilder
from boba.settings import DictConfigSource
from boba.tools import Scope, provides, tool

# --- Fake plugin module --------------------------------------------------- #


@tool
def fake_foo() -> str:
    """Test tool foo."""
    return "foo"


@tool
def fake_bar() -> str:
    """Test tool bar."""
    return "bar"


@provides(scope=Scope.APP)
def fake_provider() -> str:
    """Test provider — даёт str."""
    return "provided"


def _fake_module(name: str = "fake_plugin") -> types.ModuleType:
    """Сконструировать in-memory модуль с двумя @tool и одним @provides."""
    mod = types.ModuleType(name)
    # ModuleType — динамический контейнер атрибутов; pyright не видит их в
    # type-stub'е, потому setattr через явный list.
    for attr_name, attr_value in (
        ("fake_foo", fake_foo),
        ("fake_bar", fake_bar),
        ("fake_provider", fake_provider),
    ):
        setattr(mod, attr_name, attr_value)
    return mod


# --- Fake EntryPoint ------------------------------------------------------ #


@dataclass(frozen=True)
class _FakeEntryPoint:
    """Минимальный stub `importlib.metadata.EntryPoint`."""

    name: str
    module: types.ModuleType

    def load(self) -> types.ModuleType:
        return self.module


@pytest.fixture
def mock_entry_points(monkeypatch: pytest.MonkeyPatch):
    """Подменяет `importlib.metadata.entry_points` в builder-модуле.

    Возвращает функцию `set_eps(eps_by_group: dict[str, list[_FakeEntryPoint]])`
    которая настраивает мок.
    """

    def _setup(eps_by_group: dict[str, list[_FakeEntryPoint]]) -> None:
        def _entry_points(*, group: str) -> Iterable[_FakeEntryPoint]:
            return eps_by_group.get(group, [])

        monkeypatch.setattr(
            "boba.agent.builder.importlib.metadata.entry_points",
            _entry_points,
        )

    return _setup


# --- Helpers -------------------------------------------------------------- #


def _minimal_builder() -> AgentBuilder:
    """Builder без LLM/turn — для проверки только tool/provider регистрации."""
    return AgentBuilder()


def _tool_names(ab: AgentBuilder) -> list[str]:
    return sorted(t.plan.name for t in ab.di.tools)


def _provider_return_types(ab: AgentBuilder) -> list[type]:
    return [p.plan.return_type for p in ab.di.providers]


# --- Tests ---------------------------------------------------------------- #


def test_disabled_plugin_loads_nothing(mock_entry_points):
    """`enable=False` (или отсутствует) — модуль не импортируется."""
    fake = _fake_module()
    load_spy = MagicMock(return_value=fake)
    # `_FakeEntryPoint` — frozen dataclass; для подмены `load` оборачиваем
    # в SimpleNamespace, чтобы атрибут стал mutable. Контракт duck-typing
    # для `discover_plugins` — это `.name` + `.load()`.
    ep = types.SimpleNamespace(name="myplug", load=load_spy)
    mock_entry_points({"test.group": [ep]})

    baseline = _provider_return_types(_minimal_builder())
    ab = (
        _minimal_builder()
        .use_config(DictConfigSource({}))
        .discover_plugins(entry_point="test.group")
    )

    assert _tool_names(ab) == []
    # Сравниваем с baseline — `_minimal_builder()` сам регистрирует
    # дефолтные internal-провайдеры (HistoryService, AgentBuilderConfig, LLM).
    # Плагин-уровневых новых быть не должно.
    assert _provider_return_types(ab) == baseline
    load_spy.assert_not_called()


def test_enabled_plugin_loads_all_tools_and_providers(mock_entry_points):
    """`enable=True` без allowlist — регистрируются все tools + provider."""
    fake = _fake_module()
    ep = _FakeEntryPoint(name="myplug", module=fake)
    mock_entry_points({"test.group": [ep]})

    source = DictConfigSource({"tool.myplug": {"enable": True}})
    ab = (
        _minimal_builder()
        .use_config(source)
        .discover_plugins(
            entry_point="test.group",
        )
    )

    assert _tool_names(ab) == ["fake_bar", "fake_foo"]
    assert str in _provider_return_types(ab)


def test_allowlist_filters_tools_only(mock_entry_points):
    """`tools=[...]` пропускает только указанные @tool; @provides не трогает."""
    fake = _fake_module()
    ep = _FakeEntryPoint(name="myplug", module=fake)
    mock_entry_points({"test.group": [ep]})

    source = DictConfigSource(
        {
            "tool.myplug": {"enable": True, "tools": ["fake_foo"]},
        }
    )
    ab = (
        _minimal_builder()
        .use_config(source)
        .discover_plugins(
            entry_point="test.group",
        )
    )

    assert _tool_names(ab) == ["fake_foo"]
    # Provider всё равно регистрируется (allowlist на него не действует)
    assert str in _provider_return_types(ab)


def test_csv_string_tools_allowlist(mock_entry_points):
    """`tools` как CSV-строка (из env-var) парсится в список."""
    fake = _fake_module()
    ep = _FakeEntryPoint(name="myplug", module=fake)
    mock_entry_points({"test.group": [ep]})

    # CSV-строка имитирует env-var BOBA_TOOL__MYPLUG__TOOLS=fake_foo,fake_bar
    source = DictConfigSource(
        {
            "tool.myplug": {"enable": True, "tools": "fake_foo,fake_bar"},
        }
    )
    ab = (
        _minimal_builder()
        .use_config(source)
        .discover_plugins(
            entry_point="test.group",
        )
    )

    assert _tool_names(ab) == ["fake_bar", "fake_foo"]


def test_bool_string_enable_parsed(mock_entry_points):
    """`enable` как строка `"true"` (из env-var) корректно парсится в True."""
    fake = _fake_module()
    ep = _FakeEntryPoint(name="myplug", module=fake)
    mock_entry_points({"test.group": [ep]})

    source = DictConfigSource({"tool.myplug": {"enable": "true"}})
    ab = (
        _minimal_builder()
        .use_config(source)
        .discover_plugins(
            entry_point="test.group",
        )
    )

    assert _tool_names(ab) == ["fake_bar", "fake_foo"]


def test_multiple_plugins_independent_gates(mock_entry_points):
    """Каждый плагин гейтится своей секцией `[tool.<ep.name>]` независимо."""
    fake_a = _fake_module("plug_a")
    fake_b = _fake_module("plug_b")
    eps = [
        _FakeEntryPoint(name="plug_a", module=fake_a),
        _FakeEntryPoint(name="plug_b", module=fake_b),
    ]
    mock_entry_points({"test.group": eps})

    source = DictConfigSource(
        {
            "tool.plug_a": {"enable": True, "tools": ["fake_foo"]},
            # plug_b — не enabled
        }
    )
    ab = (
        _minimal_builder()
        .use_config(source)
        .discover_plugins(
            entry_point="test.group",
        )
    )

    # Только plug_a.fake_foo
    assert _tool_names(ab) == ["fake_foo"]
