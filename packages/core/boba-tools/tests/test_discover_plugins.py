"""Unit-тесты `ToolBuilder.discover_plugins` — filter-driven discovery.

`PluginToolFilter` решает, какие плагины и их tool'ы попадут в реестр.
Проверяем на реальных фильтрах (`PluginFilterAllowAll`,
`PluginToolAllowListFilter`):
  - allow-all → все `@tool` регистрируются;
  - allowlist → только указанные `@tool`;
  - чужой плагин в allowlist → tool'ов нет и `@provides` тоже нет (плагин
    выключен целиком);
  - включённый плагин с пустым allowlist → tool'ов нет, но `@provides`
    остаются (фильтр — про tools, не про весь плагин);
  - каждый плагин фильтруется независимо по своему имени.

Конкретный config-driven фильтр (`[tool.<name>]` enable/tools из TOML/env) —
ответственность agent-слоя, проверяется в `boba-agent`.

Плагины подаются через реальные `importlib.metadata.EntryPoint`'ы поверх
in-memory модулей (фикстуры `make_plugin_entry_point` / `install_entry_points`).
"""

from __future__ import annotations

import pytest

from boba.tools import (
    PluginFilterAllowAll,
    PluginToolAllowListFilter,
    Scope,
    ToolBuilder,
    provides,
    tool,
)
from boba.tools.framework import ToolNameCollisionError

GROUP = "boba.tools.test"


# --- Plugin API ----------------------------------------------------------- #


@tool
def plugin_foo() -> str:
    """Test tool foo."""
    return "foo"


@tool
def plugin_bar() -> str:
    """Test tool bar."""
    return "bar"


@provides(scope=Scope.APP)
def plugin_str_provider() -> str:
    """Test provider — даёт str."""
    return "provided"


@pytest.fixture
def plugin_ep(make_plugin_entry_point):
    """EntryPoint плагина `myplug` с двумя @tool и одним @provides."""
    return make_plugin_entry_point(
        "myplug",
        {
            "plugin_foo": plugin_foo,
            "plugin_bar": plugin_bar,
            "plugin_str_provider": plugin_str_provider,
        },
    )


# --- Helpers -------------------------------------------------------------- #


def _tool_names(tb: ToolBuilder) -> list[str]:
    return sorted(t.name for t in tb.tools)


def _provider_return_types(tb: ToolBuilder) -> list[type]:
    return [p.plan.return_type for p in tb.providers]


# --- Tests ---------------------------------------------------------------- #


def test_filter_selects_all_tools(install_entry_points, plugin_ep):
    """allow-all — регистрируются все tools + provider."""
    install_entry_points(GROUP, [plugin_ep])

    tb = ToolBuilder().discover_plugins(
        GROUP,
        plugin_tool_filter=PluginFilterAllowAll(),
    )

    assert _tool_names(tb) == ["plugin_bar", "plugin_foo"]
    assert str in _provider_return_types(tb)


def test_filter_allowlist_filters_tools_only(install_entry_points, plugin_ep):
    """allowlist пропускает только указанные @tool; @provides не трогает."""
    install_entry_points(GROUP, [plugin_ep])

    tb = ToolBuilder().discover_plugins(
        GROUP,
        plugin_tool_filter=PluginToolAllowListFilter("myplug", ["plugin_foo"]),
    )

    assert _tool_names(tb) == ["plugin_foo"]
    assert str in _provider_return_types(tb)


def test_disabled_plugin_skipped_entirely(install_entry_points, plugin_ep):
    """Фильтр на чужой плагин — myplug не грузится: ни tools, ни providers."""
    install_entry_points(GROUP, [plugin_ep])

    tb = ToolBuilder().discover_plugins(
        GROUP,
        plugin_tool_filter=PluginToolAllowListFilter("other_plugin", ["plugin_foo"]),
    )

    assert _tool_names(tb) == []
    assert _provider_return_types(tb) == []


def test_enabled_empty_allowlist_keeps_providers(install_entry_points, plugin_ep):
    """Плагин включён, но allowlist пуст — tool'ов нет, @provides остаются."""
    install_entry_points(GROUP, [plugin_ep])

    tb = ToolBuilder().discover_plugins(
        GROUP,
        plugin_tool_filter=PluginToolAllowListFilter("myplug", []),
    )

    assert _tool_names(tb) == []
    assert str in _provider_return_types(tb)


def test_multiple_plugins_independent_filters(
    install_entry_points, make_plugin_entry_point
):
    """Каждый плагин фильтруется независимо по своему имени."""
    attrs = {"plugin_foo": plugin_foo, "plugin_bar": plugin_bar}
    eps = [
        make_plugin_entry_point("plug_a", attrs),
        make_plugin_entry_point("plug_b", attrs),
    ]
    install_entry_points(GROUP, eps)

    tb = ToolBuilder().discover_plugins(
        GROUP,
        # фильтр знает только plug_a → plug_b выключен целиком
        plugin_tool_filter=PluginToolAllowListFilter("plug_a", ["plugin_foo"]),
    )

    assert _tool_names(tb) == ["plugin_foo"]


def test_duplicate_tool_name_across_plugins_collides(
    install_entry_points, make_plugin_entry_point
):
    """Один tool-name в двух плагинах → ToolNameCollisionError на build()."""
    attrs = {"plugin_foo": plugin_foo}
    eps = [
        make_plugin_entry_point("plug_a", attrs),
        make_plugin_entry_point("plug_b", attrs),
    ]
    install_entry_points(GROUP, eps)

    tb = ToolBuilder().discover_plugins(
        GROUP,
        plugin_tool_filter=PluginFilterAllowAll(),
    )

    with pytest.raises(ToolNameCollisionError):
        tb.build()
