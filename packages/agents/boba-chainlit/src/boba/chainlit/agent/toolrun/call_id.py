"""Доставка tool_call_id в конвейер обёрток синтетическим полем схемы.

Поле помечено InjectedToolCallId: langchain заполняет его из ToolCall-конверта
вызова, LLM поля не видит. Тело инструмента про поле не знает — его снимает
из kwargs обвязка ToolRunLogger. С полем инструмент обязан вызываться полным
ToolCall-конвертом — так его и зовёт ToolNode агента.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, ClassVar

from langchain_core.tools import BaseTool, InjectedToolCallId, StructuredTool
from pydantic import BaseModel, create_model

__all__ = ["ToolCallIdField"]


class ToolCallIdField:
    """Синтетическое поле схемы с идентификатором вызова."""

    NAME: ClassVar[str] = "boba_tool_call_id"

    @classmethod
    def attach_all(cls, tools: Sequence[BaseTool]) -> None:
        """Добавляет поле в args_schema каждого инструмента."""
        for tool in tools:
            cls._attach(tool)

    @classmethod
    def pop(cls, kwargs: dict[str, object]) -> str:
        """Снять идентификатор из kwargs вызова; не приехал — пустая строка."""
        value = kwargs.pop(cls.NAME, None)
        if value is None:
            return ""

        return str(value)

    @classmethod
    def _attach(cls, tool: BaseTool) -> None:
        if not isinstance(tool, StructuredTool):
            return

        schema = tool.args_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            return

        if cls.NAME in schema.model_fields:
            return

        fields: dict[str, Any] = {}
        for name, info in schema.model_fields.items():
            fields[name] = (info.annotation, info)

        fields[cls.NAME] = (Annotated[str | None, InjectedToolCallId()], None)

        tool.args_schema = create_model(schema.__name__, **fields)
