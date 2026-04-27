"""Базовый класс Tool, value-объекты вызова и валидация аргументов."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from boba.domain.core.declaration import ObjectSchema
from boba.domain.core.patterns import (
    Converter,
    ConverterInputError,
    Definition,
    Executor,
)
from boba.domain.core.tools.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
)
from boba.domain.core.tools.ids import ToolId, ToolSourceId
from boba.domain.core.validators import MISSING
from boba.domain.core.workspace import ProjectWorkspaceShell

TArgs = TypeVar("TArgs")


@dataclass(frozen=True)
class ToolContext:
    """Per-request контекст исполнения tool'а."""

    project_workspace: ProjectWorkspaceShell


@dataclass(frozen=True)
class ToolCall:
    """Запрос на вызов инструмента."""

    tool_id: ToolId
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """Результат успешного выполнения инструмента."""

    content: str


class Tool(
    Executor[ToolContext, TArgs, ToolResult],
    Definition[ObjectSchema[dict[str, Any]]],
    Generic[TArgs],
):
    """Базовый класс tool'а; application-singleton, per-session состояние — через ToolContext."""

    @abstractmethod
    def tool_id(self) -> ToolId: ...

    @abstractmethod
    def tool_source_id(self) -> ToolSourceId: ...

    @abstractmethod
    def typed_args_converter(self) -> Converter[dict[str, Any], TArgs]:
        """Маппер провалидированного dict в типизированный TArgs."""
        ...

    def args_converter(self) -> Converter[dict[str, Any], TArgs]:
        """Pipeline: валидация по схеме → маппинг в TArgs."""
        return _ToolArgsPipeline(
            SchemaArgsValidator(self.definition(), self.tool_id()),
            self.typed_args_converter(),
        )


class _ToolArgsPipeline(Converter[dict[str, Any], TArgs], Generic[TArgs]):
    """Pipeline валидации + маппинга для args_converter."""

    def __init__(
        self,
        validator: SchemaArgsValidator,
        typed: Converter[dict[str, Any], TArgs],
    ) -> None:
        self._validator = validator
        self._typed = typed

    def convert(self, value: dict[str, Any]) -> TArgs:
        validated = self._validator.convert(value)
        return self._typed.convert(validated)


class SchemaArgsValidator(Converter[dict[str, Any], dict[str, Any]]):
    """Валидирует сырой dict аргументов против ObjectSchema; tool-specific ошибки."""

    def __init__(
        self,
        schema: ObjectSchema[dict[str, Any]],
        tool_id: ToolId,
    ) -> None:
        self._schema = schema
        self._tool_id = tool_id
        self._known: frozenset[str] = frozenset(p.name for p in schema.fields)

    def convert(self, value: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(value.keys()) - self._known)
        if unknown:
            raise InvalidToolArgumentError(
                self._tool_id,
                unknown[0],
                f"неизвестный параметр (известные: {sorted(self._known)})",
            )

        result: dict[str, Any] = {}
        for param in self._schema.fields:
            raw = value.get(param.name, MISSING)
            try:
                validated = param.converter.convert(raw)
            except ConverterInputError as e:
                raise InvalidToolArgumentError(
                    self._tool_id, param.name, str(e)
                ) from e
            if validated is not MISSING:
                result[param.name] = validated

        try:
            final = self._schema.invariants.convert(result)
        except ConverterInputError as e:
            raise InvalidSchemaInvariantError(self._tool_id, str(e)) from e
        return self._schema.factory(**final)
