"""Поля приложения в схеме инструмента: подпись вызова и тред.

Подпись (ToolIntent) заполняет LLM: одна строка о том, что делает вызов; без
неё шаг ленты называется именем инструмента. Тело инструмента про поле не
знает — его снимает из аргументов вызова обвязка ToolRunLogger. Тред —
необязательный параметр с пометкой SchemaMark.THREAD у инструментов, которые
работают в workspace треда: наш клиент прячет его от модели и подставляет
тред сам, чужой клиент показывает модели. Сервер берёт из него область
вызова; телу он не уходит — аргументы тела собираются по его схеме.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, ClassVar

from pydantic import BaseModel, Field

from boba.toolkit.calls import SchemaMark, ToolIntent
from boba.toolrun.hosted import HostedTool, ToolSchema

__all__ = ["CallFields"]


class CallFields:
    """Ставит поля приложения — подпись вызова и тред — в схему инструмента.

    Создаётся цепочкой обвязок (ToolChain), источником значений контекста
    (CallContextValues), инструментом-связкой (WorkflowTool) и операцией
    загрузки файла сервера: первые два добавляют поля в схемы инструментов
    хоста, остальные — в схему своего вызова. Сервер читает тред вызова по
    имени THREAD.
    """

    THREAD: ClassVar[str] = "thread_id"

    THREAD_DESCRIPTION: ClassVar[str] = (
        "Identifier of the current chat thread. Calls with the same thread_id "
        "share files in the workspace, so use one value for all calls of a "
        "thread. Omit it to work in your personal workspace."
    )

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

    def threaded(self, schema: type[BaseModel]) -> type[BaseModel]:
        """Схема с необязательным параметром треда; уже с ним — как есть."""
        if self.THREAD in schema.model_fields:
            return schema

        declared = Field(
            description=self.THREAD_DESCRIPTION,
            json_schema_extra={SchemaMark.THREAD.value: True},
        )
        fields = {self.THREAD: (Annotated[str, declared], "")}

        return self._schemas.rebuild(schema, fields, ())
