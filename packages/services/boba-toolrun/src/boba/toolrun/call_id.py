"""Подпись вызова в схеме инструмента.

Подпись (ToolIntent) заполняет LLM: одна строка о том, что делает вызов; без
неё шаг ленты называется именем инструмента. Тело инструмента про поле не
знает — его снимает из аргументов вызова обвязка ToolRunLogger.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from pydantic import BaseModel, Field

from boba.toolkit.calls import ToolIntent
from boba.toolrun.hosted import HostedTool, ToolSchema

__all__ = ["CallFields"]


class CallFields:
    """Ставит поле подписи вызова в схему инструмента.

    Создаётся цепочкой обвязок (ToolChain) и инструментом-связкой
    (WorkflowTool): первая добавляет поле в схемы инструментов одним
    проходом, второй — в схему своего вызова.
    """

    def __init__(self) -> None:
        self._schemas = ToolSchema()

    def attach_all(self, tools: Sequence[HostedTool]) -> None:
        """Добавляет поле в args_schema каждого инструмента."""
        for tool in tools:
            tool.args_schema = self.extended(tool.args_schema)

    def extended(self, schema: type[BaseModel]) -> type[BaseModel]:
        """Схема с полем подписи вызова; уже подписанная — как есть."""
        if ToolIntent.NAME in schema.model_fields:
            return schema

        # необязательное: инструмент без своих аргументов модель зовёт с {},
        # и обязательная подпись роняла бы вызов. Потолок длины держит показ
        declared = Field(description=ToolIntent.DESCRIPTION)
        fields = {ToolIntent.NAME: (Annotated[str, declared], "")}

        return self._schemas.rebuild(schema, fields, ())
