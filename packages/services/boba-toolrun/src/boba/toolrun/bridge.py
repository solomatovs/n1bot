"""Мост TOOLS модулей инструментов в langchain: toolkit langchain не знает.

Ошибки:
TypeError — элемент TOOLS не PayloadTool и не langchain BaseTool, либо тело
    вернуло не модель результата ToolResultBase.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from functools import wraps
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from boba.toolkit.entry import ToolAddress, ToolLike
from boba.toolkit.facade import PayloadTool
from boba.toolkit.result import ToolResultBase

__all__ = ["ToolBridge"]


class ToolBridge:
    """PayloadTool фасада -> StructuredTool langchain с парой (content, artifact).

    Реестр инструментов и стенды собирают через него TOOLS модулей;
    langchain-инструмент проходит как есть.
    """

    @classmethod
    def as_structured_tool(cls, tool: ToolLike) -> BaseTool:
        """PayloadTool фасада -> StructuredTool; langchain-инструмент — как есть.

        Injected-параметры остаются в args_schema: их снимает InjectedConfig
        после постановки обёртки запуска, LLM усечённую схему и увидит.
        """
        if isinstance(tool, BaseTool):
            return tool

        if not isinstance(tool, PayloadTool):
            msg = (
                f"module tool {tool!r}: expected PayloadTool or langchain "
                f"BaseTool, got {type(tool).__name__}"
            )
            raise TypeError(msg)

        func = None
        if tool.func is not None:
            func = cls._packed(tool, tool.func)

        coroutine = None
        if tool.coroutine is not None:
            coroutine = cls._packed_async(tool, tool.coroutine)

        return StructuredTool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            func=func,
            coroutine=coroutine,
            response_format=PayloadTool.RESPONSE_FORMAT,
        )

    @classmethod
    def _packed(
        cls, tool: PayloadTool, body: Callable[..., Any]
    ) -> Callable[..., tuple[str, ToolResultBase]]:
        """Тело, отдающее модель, -> тело с парой (content, artifact) langchain.

        Аргументы langchain приходят по отдельности: тело с классом вызова
        получает их его экземпляром. wraps сохраняет исходное тело в
        __wrapped__: адрес запуска читает оттуда модуль и аннотацию результата.
        """

        @wraps(body)
        def call(**kwargs: Any) -> tuple[str, ToolResultBase]:
            return cls._pack(tool.name, body(**tool.packed_kwargs(kwargs)))

        return call

    @classmethod
    def _packed_async(
        cls, tool: PayloadTool, body: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[tuple[str, ToolResultBase]]]:
        @wraps(body)
        async def call(**kwargs: Any) -> tuple[str, ToolResultBase]:
            return cls._pack(tool.name, await body(**tool.packed_kwargs(kwargs)))

        return call

    @staticmethod
    def _pack(name: str, result: object) -> tuple[str, ToolResultBase]:
        if not isinstance(result, ToolResultBase):
            msg = (
                f"tool {name!r} must return a ToolResultBase model, "
                f"got {type(result).__name__}"
            )
            raise TypeError(msg)

        return result.packed()

    @classmethod
    def toolset(cls, tools: Sequence[ToolLike]) -> tuple[BaseTool, ...]:
        """TOOLS модуля инструментов -> langchain-инструменты для реестра."""
        checked: list[BaseTool] = []
        for tool in tools:
            checked.append(cls.as_structured_tool(tool))

        return tuple(checked)

    @staticmethod
    def modules_of(tools: Sequence[ToolLike]) -> tuple[str, ...]:
        """Уникальные модули тел, в порядке объявления."""
        modules: list[str] = []
        for tool in tools:
            module = ToolAddress.of(tool).module
            if module not in modules:
                modules.append(module)

        return tuple(modules)
