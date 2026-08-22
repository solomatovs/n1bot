"""Подпись вызова для ленты синтетическим полем схемы.

Поле ToolIntent.NAME добавляется в args_schema каждого инструмента обязательным,
и его заполняет LLM: одна строка о том, что делает вызов. Тело инструмента про
поле не знает — его снимает из kwargs обвязка ToolRunLogger, как и tool_call_id.
Вызов без подписи отклоняет валидация схемы: ошибка уходит модели тем же
конвертом tool_result, что и любая другая, и ход продолжается.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, create_model

from boba.toolkit.calls import ToolIntent

__all__ = ["ToolIntentField"]


class ToolIntentField:
    """Синтетическое поле схемы с подписью вызова."""

    @classmethod
    def attach_all(cls, tools: Sequence[BaseTool]) -> None:
        """Добавляет поле в args_schema каждого инструмента."""
        for tool in tools:
            cls._attach(tool)

    @classmethod
    def pop(cls, kwargs: dict[str, object]) -> str:
        """Снять подпись из kwargs вызова; не приехала — пустая строка."""
        value = kwargs.pop(ToolIntent.NAME, None)
        if not isinstance(value, str):
            return ""

        return value

    @classmethod
    def _attach(cls, tool: BaseTool) -> None:
        if not isinstance(tool, StructuredTool):
            return

        schema = tool.args_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            return

        if ToolIntent.NAME in schema.model_fields:
            return

        fields: dict[str, Any] = {}
        for name, info in schema.model_fields.items():
            fields[name] = (info.annotation, info)

        # обязательное: необязательное модель заполняла у одних инструментов и
        # пропускала у других, и лента выходила разнородной. Потолок длины
        # держит показ, а не схема: подпись не должна ронять вызов
        declared = Field(description=ToolIntent.DESCRIPTION)
        fields[ToolIntent.NAME] = (Annotated[str, declared], ...)

        tool.args_schema = create_model(schema.__name__, **fields)
