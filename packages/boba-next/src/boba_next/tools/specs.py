"""Specification'ы для фильтрации Tool по имени и источнику."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from boba.patterns import Specification
from boba_next.tools.tool import Tool

__all__ = ["ToolNameIn", "ToolSourceIn"]


class ToolNameIn(Specification[Tool[Any]]):
    """Allow tools, чьи имена входят в набор."""

    def __init__(self, names: Iterable[str]) -> None:
        self._names = frozenset(names)

    def check(self, candidate: Tool[Any]) -> bool:
        return candidate.tool_id().name in self._names


class ToolSourceIn(Specification[Tool[Any]]):
    """Allow tools, чьи источники (entry-point names) входят в набор."""

    def __init__(self, sources: Iterable[str]) -> None:
        self._sources = frozenset(sources)

    def check(self, candidate: Tool[Any]) -> bool:
        return candidate.tool_source_id().name in self._sources
