"""Поля приложения в схеме инструмента: подпись вызова и область вызова.

Подпись (ToolIntent) заполняет LLM: одна строка о том, что делает вызов; без
неё шаг ленты называется именем инструмента. Тело инструмента про поле не
знает — его снимает из аргументов вызова обвязка ToolRunLogger. Область —
необязательный параметр с пометкой SchemaMark.SCOPE у инструментов, которые
работают в workspace: сервер объявляет пометку расширением ScopeFeature, и
клиент с этим расширением прячет параметр от модели и ставит в него свою
область сам, клиент без расширения показывает его модели. Сервер берёт из
него область вызова; телу он не уходит — аргументы тела собираются по его
схеме.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, ClassVar

from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from boba.toolkit.calls import SchemaMark, ToolIntent
from boba.toolkit.wire import ScopeFeature
from boba.toolrun.hosted import HostedTool, ToolSchema

__all__ = ["CallFields"]


class CallFields:
    """Ставит поля приложения — подпись вызова и область вызова — в схему
    инструмента.

    Создаётся цепочкой обвязок (ToolChain), источником значений контекста
    (CallContextValues), инструментом-связкой (WorkflowTool) и операцией
    загрузки файла сервера: первые два добавляют поля в схемы инструментов
    хоста, остальные — в схему своего вызова. Сервер читает область вызова
    по имени SCOPE и объявляет её пометку клиентам расширением feature().
    """

    SCOPE: ClassVar[str] = "scope_id"

    SCOPE_DESCRIPTION: ClassVar[str] = (
        "Идентификатор области вызова:\n"
        "   - id области — вызовы с одним scope_id видят файлы друг друга в "
        "workspace, бери одно значение для всех вызовов одной работы\n"
        "   - пусто — работа в личном workspace пользователя\n"
    )

    def __init__(self) -> None:
        self._schemas = ToolSchema()

    def feature(self) -> ScopeFeature:
        """Расширение области вызова, которым сервер объявляет пометку
        параметра клиентам."""
        return ScopeFeature(mark=SchemaMark.SCOPE.value)

    def attach_all(self, tools: Sequence[HostedTool]) -> None:
        """Добавляет поле в args_schema каждого инструмента."""
        for tool in tools:
            tool.args_schema = self.extended(tool.args_schema)

    def extended(self, schema: type[BaseModel]) -> type[BaseModel]:
        """Схема с полем подписи вызова; уже подписанная — как есть."""
        # необязательное: инструмент без своих аргументов модель зовёт с {},
        # и обязательная подпись роняла бы вызов. Потолок длины держит показ
        declared = Field(description=ToolIntent.DESCRIPTION)

        return self._added(schema, ToolIntent.NAME, declared)

    def scoped(self, schema: type[BaseModel]) -> type[BaseModel]:
        """Схема с необязательным параметром области вызова; уже с ним — как
        есть."""
        declared = Field(
            description=self.SCOPE_DESCRIPTION,
            json_schema_extra={SchemaMark.SCOPE.value: True},
        )

        return self._added(schema, self.SCOPE, declared)

    def _added(
        self, schema: type[BaseModel], name: str, declared: FieldInfo
    ) -> type[BaseModel]:
        """Схема с необязательной строкой name, объявленной declared; уже с
        ней — как есть."""
        if name in schema.model_fields:
            return schema

        fields = {name: (Annotated[str, declared], "")}

        return self._schemas.rebuild(schema, fields, ())
