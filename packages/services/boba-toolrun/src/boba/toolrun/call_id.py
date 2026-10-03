"""Служебные поля вызова в схеме инструмента: идентификатор вызова и подпись.

Идентификатор помечен InjectedToolCallId: langchain заполняет его из
ToolCall-конверта вызова, LLM поля не видит. Подпись (ToolIntent) заполняет
LLM: одна строка о том, что делает вызов; без неё шаг ленты называется
именем инструмента. Тело инструмента про оба поля не знает — их снимает из
kwargs обвязка ToolRunLogger. С полем идентификатора инструмент обязан
вызываться полным ToolCall-конвертом — так его и зовёт ToolNode агента.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, ClassVar

from langchain_core.tools import BaseTool, InjectedToolCallId
from pydantic import Field

from boba.toolkit.calls import ToolIntent
from boba.toolrun.wrapping import ToolSchema

__all__ = ["CallFields"]


class CallFields:
    """Ставит служебные поля вызова в схему инструмента и снимает id из вызова.

    Создаётся цепочкой обвязок (ToolChain) и обвязкой журнала
    (ToolRunLogger): первая добавляет поля в схему одним проходом, вторая
    читает идентификатор из аргументов вызова.
    """

    CALL_ID: ClassVar[str] = "boba_tool_call_id"

    def attach_all(self, tools: Sequence[BaseTool]) -> None:
        """Добавляет поля в args_schema каждого инструмента."""
        for tool in tools:
            self._attach(tool)

    def call_id(self, kwargs: dict[str, object]) -> str:
        """Снять идентификатор из kwargs вызова; не приехал — пустая строка."""
        value = kwargs.pop(self.CALL_ID, None)
        if value is None:
            return ""

        return str(value)

    def _attach(self, tool: BaseTool) -> None:
        schema = ToolSchema.of(tool)
        if schema is None:
            return

        fields: dict[str, tuple[Any, Any]] = {}
        if self.CALL_ID not in schema.model_fields:
            fields[self.CALL_ID] = (Annotated[str | None, InjectedToolCallId()], None)

        if ToolIntent.NAME not in schema.model_fields:
            # необязательное: инструмент без своих аргументов модель зовёт с {},
            # и обязательная подпись роняла бы вызов. Потолок длины держит показ
            declared = Field(description=ToolIntent.DESCRIPTION)
            fields[ToolIntent.NAME] = (Annotated[str, declared], "")

        if not fields:
            return

        tool.args_schema = ToolSchema.rebuild(schema, fields, ())
