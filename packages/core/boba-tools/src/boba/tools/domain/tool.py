"""Базовый класс Tool, value-объекты вызова и tool-specific обёртка args-конвертации."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Generic, TypeVar

from boba.patterns import Converter, Definition, Executor
from boba.schema.declaration import (
    FieldPathError,
    FieldPathMissingError,
    ObjectSchema,
)
from boba.tools.domain.args import ToolArgsBuilder
from boba.tools.domain.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
)
from boba.tools.domain.ids import ToolId, ToolName, ToolSourceId
from boba.tools.domain.result import ToolResult
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["Tool", "ToolCall", "ToolContext", "ToolResult"]

TArgs = TypeVar("TArgs")


@dataclass(frozen=True)
class ToolContext:
    """Per-request контекст исполнения tool'а."""

    project_workspace: ProjectWorkspaceShell


@dataclass(frozen=True)
class ToolCall:
    """Запрос на вызов инструмента (qualified wire-id `<source>/<name>`)."""

    tool_id: ToolId
    arguments: dict[str, Any]


class Tool(
    Executor[ToolContext, TArgs, ToolResult],
    Definition[ObjectSchema[TArgs]],
    Generic[TArgs],
):
    """Базовый класс tool'а; application-singleton.

    Identity: каждый Tool владеет своим qualified `ToolId` (`<source>/<name>`).
    `name()` и `source_id()` — derived из `tool_id()`.

    Два публичных слоя вызова:
    - `execute(ctx, args: TArgs)` — типизированное тело; implementor пишет это.
    - `invoke(ctx, raw: dict)` — boundary entry: парсит `raw` через `definition()`
      и зовёт `execute`. Это то, что зовёт `ToolsService` после dispatch'а.

    Adapter (`_ToolArgsAdapter`) кэшируется в `cached_property` — schema
    строится через `definition()` один раз на инстанс.
    """

    @abstractmethod
    def tool_id(self) -> ToolId: ...

    def name(self) -> ToolName:
        return self.tool_id().parse()[1]

    def source_id(self) -> ToolSourceId:
        return self.tool_id().parse()[0]

    def invoke(self, ctx: ToolContext, raw: dict[str, Any]) -> ToolResult:
        """Распарсить `raw` через `definition()` и делегировать в `execute`."""
        args = self._args_adapter.convert(raw)
        return self.execute(ctx, args)

    @cached_property
    def _args_adapter(self) -> _ToolArgsAdapter[TArgs]:
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
            raise InvalidToolArgumentError(
                self._tool_id, e.field_name, str(e),
            ) from e
        except FieldPathError as e:
            if e.field_name == "<invariants>":
                raise InvalidSchemaInvariantError(self._tool_id, str(e)) from e
            raise InvalidToolArgumentError(
                self._tool_id, e.field_name, str(e),
            ) from e
