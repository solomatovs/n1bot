"""Базовый класс Tool, value-объекты вызова и tool-specific обёртка args-конвертации."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from typing import (
    Any,
    Generic,
    Protocol,
    TypeVar,
    get_args,
    get_origin,
    runtime_checkable,
)

from boba.patterns import Converter, Definition, Executor
from boba.schema.declaration import (
    FieldPathError,
    FieldPathMissingError,
    ObjectSchema,
)
from boba.schema.from_dataclass import schema_from_dataclass
from boba.tools.domain.args import ToolArgsBuilder
from boba.tools.domain.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
)
from boba.tools.domain.ids import ToolId, ToolName, ToolSourceId
from boba.tools.domain.result import ToolResult
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = [
    "SchemaOverlay",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolResult",
]

TArgs = TypeVar("TArgs")
TConfig = TypeVar("TConfig")


@runtime_checkable
class SchemaOverlay(Protocol):
    """Структурный protocol overlay'я: умеет применить себя к ObjectSchema.

    Реализуется любым объектом с методом `apply(schema) -> schema` —
    в частности `boba.plugin.prompt.PromptOverlay`. Domain-слой не зависит
    от plugin-слоя; protocol даёт duck-typed расширение без жёсткой связи.
    """

    def apply(self, schema: ObjectSchema[Any]) -> ObjectSchema[Any]: ...


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
    Generic[TArgs, TConfig],
):
    """Базовый класс tool'а; application-singleton.

    Соглашения по subclass'ам:
    - наследуют `Tool[XArgs, XToolConfig]` (оба type-var зафиксированы);
    - реализуют только `execute(ctx, args: TArgs) -> ToolResult`;
    - `name()`, `tool_id()`, `definition()` имеют дефолтные реализации
      на базе TArgs/TConfig и могут быть переопределены.

    Соглашения по cfg:
    - конфиг хранится в `self._cfg`;
    - если у конфига есть атрибут `prompt`, реализующий `SchemaOverlay`,
      он автоматически применяется к авто-сгенерированной схеме.

    Identity: tool_id = `<source>/<name>`. По умолчанию `name()` —
    snake_case имени класса без суффикса `Tool` (CatTool → "cat").
    """

    _cfg: TConfig

    def __init__(
        self,
        cfg: TConfig,
        ctx: Any,
        source_id: ToolSourceId,
    ) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._source_id = source_id

    @cached_property
    def _tool_id(self) -> ToolId:
        return ToolId.compose(self._source_id, self.name())

    def tool_id(self) -> ToolId:
        return self._tool_id

    def name(self) -> ToolName:
        """Локальное имя tool'а; default — snake_case класса без суффикса 'Tool'."""
        cls_name = type(self).__name__
        if cls_name.endswith("Tool"):
            cls_name = cls_name[:-4]
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", cls_name).lower()
        return ToolName(snake)

    def source_id(self) -> ToolSourceId:
        return self._source_id

    def definition(self) -> ObjectSchema[TArgs]:
        """Wire-schema параметров: автоген из dataclass TArgs + cfg.prompt overlay.

        TArgs резолвится из объявления `Tool[XArgs, XConfig]` через `__orig_bases__`.
        Если у `self._cfg` есть атрибут `prompt`, реализующий `SchemaOverlay`,
        он применяется поверх схемы; иначе схема возвращается как есть.
        """
        schema = schema_from_dataclass(self._resolve_args_type())
        prompt = getattr(self._cfg, "prompt", None)
        if isinstance(prompt, SchemaOverlay):
            return prompt.apply(schema)
        return schema

    @classmethod
    def _resolve_args_type(cls) -> type:
        """Извлечь параметр TArgs из объявления `Tool[XArgs, XConfig]`."""
        for klass in cls.__mro__:
            for base in getattr(klass, "__orig_bases__", ()):
                origin = get_origin(base)
                if isinstance(origin, type) and issubclass(origin, Tool):
                    targs = get_args(base)
                    if targs and isinstance(targs[0], type):
                        return targs[0]
        msg = (
            f"{cls.__name__}: не удалось определить TArgs; "
            f"наследуй от Tool[XArgs, XConfig] с конкретным dataclass-типом."
        )
        raise TypeError(msg)

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
