"""Подстановка Injected-конфига в kwargs вызова и правка схемы инструмента.

Ставится сразу поверх обёртки запуска: partial кладёт значения в kwargs до
того, как обёртка разложит их на argv и stdin. Заодно injected-поля снимаются
с args_schema — langchain валидирует вход по полной схеме до тела, и
обязательный injected-параметр без значения ронял бы вызов.

Ошибки: ToolConfigError — у injected-параметра нет значения у загрузчика.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, TypeAlias

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, create_model

from boba.chainlit.agent.toolrun.wrapping import CallHooks, ToolBody
from boba.toolkit.entry import ToolArgv

__all__ = ["ConfigResolver", "InjectedConfig", "ToolConfigError"]

ConfigResolver: TypeAlias = Callable[[str, Any], object]
"""(имя параметра, аннотация) -> значение; собирает загрузчик из конфига."""


class ToolConfigError(Exception):
    """Injected-параметру инструмента нечего подставить."""


class InjectedConfig:
    """Партиал конфига поверх обёртки запуска плюс снятие полей со схемы."""

    class _Partial(CallHooks):
        def __init__(self, values: dict[str, object]) -> None:
            self._values = values

        def before(
            self,
            name: str,
            args: tuple[object, ...],
            kwargs: dict[str, object],
        ) -> object:
            for key, value in self._values.items():
                kwargs.setdefault(key, value)

            return None

    @classmethod
    def bind_all(cls, tools: Sequence[BaseTool], resolve: ConfigResolver) -> None:
        for tool in tools:
            cls._bind(tool, resolve)

    @classmethod
    def _bind(cls, tool: BaseTool, resolve: ConfigResolver) -> None:
        if not isinstance(tool, StructuredTool):
            return

        schema = tool.args_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            return

        injected = ToolArgv.injected_fields(schema)
        if not injected:
            return

        values: dict[str, object] = {}
        for name, annotation in injected.items():
            values[name] = resolve(name, annotation)

        ToolBody.hook_all([tool], cls._Partial(values))
        tool.args_schema = cls._without(schema, set(injected))

    @staticmethod
    def _without(schema: type[BaseModel], names: set[str]) -> type[BaseModel]:
        """Схема без injected-полей: вход от LLM валидируется по остатку."""
        fields: dict[str, Any] = {}
        for name, info in schema.model_fields.items():
            if name in names:
                continue

            fields[name] = (info.annotation, info)

        return create_model(schema.__name__, **fields)
