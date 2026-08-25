"""Подпись вызова для ленты синтетическим полем схемы.

Поле ToolIntent.NAME добавляется в args_schema каждого инструмента; его
заполняет LLM: одна строка о том, что делает вызов. Тело инструмента про поле
не знает — его снимает из kwargs обвязка ToolRunLogger (ToolIntent.pop). Поле
необязательное: без подписи шаг ленты называется именем инструмента.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from langchain_core.tools import BaseTool
from pydantic import Field

from boba.chainlit.agent.toolrun.wrapping import ToolSchema
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
    def _attach(cls, tool: BaseTool) -> None:
        schema = ToolSchema.of(tool)
        if schema is None:
            return

        if ToolIntent.NAME in schema.model_fields:
            return

        # необязательное: инструмент без своих аргументов модель зовёт с {},
        # и обязательная подпись роняла бы вызов. Потолок длины держит показ
        declared = Field(description=ToolIntent.DESCRIPTION)
        field = (Annotated[str, declared], "")
        tool.args_schema = ToolSchema.rebuild(schema, {ToolIntent.NAME: field}, ())
