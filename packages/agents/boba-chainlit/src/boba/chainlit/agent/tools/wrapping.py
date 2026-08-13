"""Постановка обёрток на тело langchain-инструмента.

Обвязки (логи, права, отмена, тап потока, ошибки) отличаются только телом
обёртки, а способ её постановки один: у инструмента два тела — sync-функция и
корутина, — и обёртка должна лечь на оба. Инструменты приложения строит
декоратор `@tool`, то есть это StructuredTool; тула другого класса обвязка не
касается — подменять там нечего.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeAlias

from langchain_core.tools import BaseTool, StructuredTool

__all__ = ["AsyncCall", "SyncCall", "ToolBody"]

SyncCall: TypeAlias = Callable[..., Any]
"""Sync-тело инструмента: аргументы задаёт схема самого тула."""

AsyncCall: TypeAlias = Callable[..., Awaitable[Any]]
"""Async-тело инструмента."""


class ToolBody:
    """Тела инструментов: обёртка ставится на sync и async вместе."""

    @staticmethod
    def wrap_all(
        tools: Sequence[BaseTool],
        wrap: Callable[[SyncCall, str], SyncCall],
        wrap_async: Callable[[AsyncCall, str], AsyncCall],
    ) -> list[BaseTool]:
        """Оборачивает тела каждого инструмента; порядок списка сохраняется."""
        wrapped: list[BaseTool] = []
        for tool in tools:
            wrapped.append(tool)

            if not isinstance(tool, StructuredTool):
                continue

            if tool.func is not None:
                tool.func = wrap(tool.func, tool.name)

            if tool.coroutine is not None:
                tool.coroutine = wrap_async(tool.coroutine, tool.name)

        return wrapped
