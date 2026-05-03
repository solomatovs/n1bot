"""Базовый класс Tool, value-объекты вызова и tool-specific обёртка args-конвертации."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from boba.declaration import (
    FieldPathError,
    FieldPathMissingError,
    ObjectSchema,
)
from boba.patterns import Converter, Definition, Executor
from boba.rendering import ToolResult
from boba.tools.args import ToolArgsBuilder
from boba.tools.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
)
from boba.tools.ids import ToolId, ToolSourceId
from boba.workspace import ProjectWorkspaceShell

__all__ = ["Tool", "ToolCall", "ToolContext", "ToolResult"]

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


class Tool(
    Executor[ToolContext, TArgs, ToolResult],
    Definition[ObjectSchema[TArgs]],
    Generic[TArgs],
):
    """Базовый класс tool'а; application-singleton.

    `definition()` возвращает `ObjectSchema[TArgs]` — схема **сама** строит
    типизированный DTO через `factory=TArgs` (dataclass-класс или ctor).
    Tool НЕ нуждается в ручном `typed_args_converter` — `args_converter()`
    использует `ToolArgsBuilder` поверх `definition()`.
    """

    @abstractmethod
    def tool_id(self) -> ToolId: ...

    @abstractmethod
    def tool_source_id(self) -> ToolSourceId: ...

    def args_converter(self) -> Converter[dict[str, Any], TArgs]:
        """Tool-specific обёртка ToolArgsBuilder с unknown-keys-check."""
        return _ToolArgsAdapter(self.definition(), self.tool_id())


class _ToolArgsAdapter(Converter[dict[str, Any], TArgs], Generic[TArgs]):
    """
    Адаптер ToolArgsBuilder для Tool:
    - проверяет unknown keys и
    - переоборачивает FieldPathError в
        InvalidToolArgumentError / InvalidSchemaInvariantError.
    """

    def __init__(self, schema: ObjectSchema[TArgs], tool_id: ToolId) -> None:
        self._builder: ToolArgsBuilder[TArgs] = ToolArgsBuilder(schema)
        self._tool_id = tool_id
        self._known: frozenset[str] = frozenset(f.name for f in schema.fields)

    def convert(self, value: dict[str, Any]) -> TArgs:
        unknown = sorted(set(value.keys()) - self._known)
        if unknown:
            raise InvalidToolArgumentError(
                self._tool_id,
                unknown[0],
                f"неизвестный параметр (известные: {sorted(self._known)})",
            )
        try:
            return self._builder.build(value)
        except FieldPathMissingError as e:
            raise InvalidToolArgumentError(self._tool_id, e.field_name, str(e)) from e
        except FieldPathError as e:
            if e.field_name == "<invariants>":
                raise InvalidSchemaInvariantError(self._tool_id, str(e)) from e
            raise InvalidToolArgumentError(self._tool_id, e.field_name, str(e)) from e
