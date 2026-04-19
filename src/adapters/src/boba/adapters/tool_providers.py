"""Конкретные реализации ToolSource."""

from __future__ import annotations

from collections.abc import Iterable

from boba.domain.core.tools import Tool, ToolSource, ToolSourceId


class StaticToolSource(ToolSource):
    """Фиксированный набор ``Tool``, зашитый в код.

    Подходит для builtin-инструментов: создаёшь список Tool-ов, оборачиваешь
    в этот источник и регистрируешь в ``ToolSourceAggregator``.
    """

    def __init__(
        self,
        source_id: ToolSourceId,
        priority: int,
        tools: Iterable[Tool],
    ) -> None:
        self._id = source_id
        self._priority = priority
        self._tools = list(tools)

    def id(self) -> ToolSourceId:
        return self._id

    def priority(self) -> int:
        return self._priority

    def tools(self) -> Iterable[Tool]:
        return iter(self._tools)
