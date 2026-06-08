"""Pytest-фикстуры пакета boba-tools."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterable
from importlib.metadata import EntryPoint

import pytest

from boba.tools.domain import ToolContext, ToolSourceId


@pytest.fixture
def tool_ctx() -> ToolContext:
    """Пустой ToolContext для tool.execute(...) вызовов."""
    return ToolContext()


@pytest.fixture
def tool_source_id() -> ToolSourceId:
    """Тестовый ToolSourceId('test')."""
    return ToolSourceId("test")


@pytest.fixture
def make_plugin_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str, dict[str, object]], EntryPoint]:
    """Фабрика реальных EntryPoint'ов поверх in-memory плагин-модулей.

    Модуль кладётся в sys.modules, поэтому настоящий EntryPoint.load()
    находит его по имени — stub не нужен. Очистка sys.modules — через
    monkeypatch.setitem.
    """

    def _make(name: str, attrs: dict[str, object]) -> EntryPoint:
        module_name = f"boba_tools_test_plugin_{name}"
        module = types.ModuleType(module_name)
        for attr, value in attrs.items():
            setattr(module, attr, value)
        monkeypatch.setitem(sys.modules, module_name, module)
        return EntryPoint(name=name, value=module_name, group="boba.tools.test")

    return _make


@pytest.fixture
def install_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str, list[EntryPoint]], None]:
    """Подменяет importlib.metadata.entry_points в builder-модуле.

    Возвращает фабрику (group, eps): для запрошенной группы отдаёт eps,
    для остальных — пусто.
    """

    def _install(served_group: str, eps: list[EntryPoint]) -> None:
        def _entry_points(*, group: str) -> Iterable[EntryPoint]:
            return eps if group == served_group else []

        monkeypatch.setattr(
            "boba.tools.declarative.builder.importlib.metadata.entry_points",
            _entry_points,
        )

    return _install
