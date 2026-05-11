"""Декоратор `@tool` и фабрика `tool_factory`: callable → `ToolDecoratorFactory`.

Тонкий слой поверх `boba.schema.schema_from_callable`: добавляет
ToolContext-инжекцию, ToolId/ToolName и обработку возвращаемого значения
функции.

Точки входа:
- `@tool` — декоратор-сахар для функций (с опциональным переопределением
  имени/описания/`parse_docstring`).
- `tool_factory(obj, ...)` — для callable-инстансов (класс с `__call__`).
- `ToolDecoratorFactory.from_callable(obj, ...)` — низкоуровневая фабрика.

ToolContext-параметр в подписи помечается как injected — в схему не попадает,
при выполнении прокидывается отдельно в `execute`.

Имя по умолчанию:
- функция        → `fn.__name__`
- callable-инстанс → `type(obj).__name__`

Тип возврата функции:
- `ToolResult` (TextResult/JsonResult) — пробрасывается as-is
- `str` — `TextResult(text=value)`
- `int` / `float` / `bool` — `TextResult(text=str(value))`
- `dict` — `JsonResult(payload=value)`
- `list` / `tuple` / `set` / `frozenset` — `JsonResult(payload=list(value))`
- dataclass-инстанс — `JsonResult(payload=dataclasses.asdict(value))`
- `None` — `TextResult(text="null")`
- прочее — `TypeError`
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, overload

from boba.schema import schema_from_callable
from boba.schema.declaration import ObjectSchema
from boba.tools.domain.ids import ToolId, ToolName, ToolSourceId
from boba.tools.domain.result import JsonResult, TextResult, ToolResult
from boba.tools.domain.tool import Tool, ToolContext
from boba.tools.framework.registry import StaticToolSource, ToolSource

__all__ = ["ToolDecoratorFactory", "tool", "tool_factory"]


class ToolDecoratorFactory:
    """Результат `@tool` / `tool_factory`: связывает callable со схемой.

    ToolId компонуется в `build(source_id)`, поэтому фабрика не привязана
    к конкретному `ToolSource`.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        name: ToolName,
        description: str,
        schema: ObjectSchema[dict[str, Any]],
        injects_ctx: bool,
    ) -> None:
        self._fn = fn
        self._name = name
        self._description = description
        self._schema = schema
        self._injects_ctx = injects_ctx

    @property
    def name(self) -> ToolName:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def schema(self) -> ObjectSchema[dict[str, Any]]:
        return self._schema

    @property
    def injects_ctx(self) -> bool:
        return self._injects_ctx

    def build(self, source_id: ToolSourceId) -> Tool[dict[str, Any], None]:
        """Привязать к source_id и вернуть готовый Tool."""
        return DecoratedTool(
            fn=self._fn,
            tool_id=ToolId.compose(source_id, self._name),
            schema=self._schema,
            injects_ctx=self._injects_ctx,
        )

    def into_source(self, source_id: ToolSourceId) -> ToolSource:
        """Обернуть в `ToolSource` из одного инструмента."""
        return StaticToolSource(source_id, [self.build(source_id)])

    @classmethod
    def from_callable(
        cls,
        obj: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        parse_docstring: bool = False,
    ) -> ToolDecoratorFactory:
        """Превратить функцию или callable-инстанс в фабрику.

        Параметр с типом `ToolContext` исключается из схемы и помечается как
        injected (передаётся в `execute` отдельно). Если в подписи больше
        одного `ToolContext`-параметра — `TypeError`.
        """
        parsed = schema_from_callable(
            obj,
            ignore_types=(ToolContext,),
            parse_docstring=parse_docstring,
        )

        if len(parsed.injected) > 1:
            msg = (
                f"@tool: параметр ToolContext должен быть один "
                f"({parsed.name!r}, повторно: {parsed.injected[1]!r})"
            )
            raise TypeError(msg)

        description_final = (
            description if description is not None else parsed.description
        )
        name_final = name if name is not None else parsed.name

        schema = parsed.schema
        if description is not None:
            schema = dataclasses.replace(schema, description=description_final)

        return cls(
            fn=obj,
            name=ToolName(name_final),
            description=description_final,
            schema=schema,
            injects_ctx=len(parsed.injected) == 1,
        )

    @classmethod
    def from_function(
        cls,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        parse_docstring: bool = False,
    ) -> ToolDecoratorFactory:
        """Алиас `from_callable` для функций (обратная совместимость)."""
        return cls.from_callable(
            fn,
            name=name,
            description=description,
            parse_docstring=parse_docstring,
        )


class DecoratedTool(Tool[dict[str, Any], None]):
    """Concrete Tool, рождённый @tool. tool_id заполнен через ToolFactory.build."""

    def __init__(
        self,
        fn: Callable[..., Any],
        tool_id: ToolId,
        schema: ObjectSchema[dict[str, Any]],
        injects_ctx: bool,
    ) -> None:
        self._fn = fn
        self._tool_id_value = tool_id
        self._schema = schema
        self._injects_ctx = injects_ctx

    def tool_id(self) -> ToolId:
        return self._tool_id_value

    def definition(self) -> ObjectSchema[dict[str, Any]]:
        return self._schema

    def execute(
        self,
        ctx: ToolContext,
        args: dict[str, Any],
    ) -> ToolResult:
        raw = self._fn(ctx, **args) if self._injects_ctx else self._fn(**args)
        return _coerce_result(self._tool_id_value, raw)


_ResultRule = tuple[Callable[[Any], bool], Callable[[Any], ToolResult]]

_RESULT_COERCERS: tuple[_ResultRule, ...] = (
    (lambda v: v is None, lambda _: TextResult(text="null")),
    (lambda v: isinstance(v, ToolResult), lambda v: v),
    (lambda v: isinstance(v, str), lambda v: TextResult(text=v)),
    (lambda v: isinstance(v, (bool, int, float)), lambda v: TextResult(text=str(v))),
    (
        lambda v: dataclasses.is_dataclass(v) and not isinstance(v, type),
        lambda v: JsonResult(payload=dataclasses.asdict(v)),
    ),
    (lambda v: isinstance(v, dict), lambda v: JsonResult(payload=v)),
    (
        lambda v: isinstance(v, (list, tuple, set, frozenset)),
        lambda v: JsonResult(payload=list(v)),
    ),
)


def _coerce_result(tool_id: ToolId, value: Any) -> ToolResult:
    """Привести возврат функции к ToolResult."""
    for predicate, convert in _RESULT_COERCERS:
        if predicate(value):
            return convert(value)
    msg = (
        f"@tool: функция {tool_id.to_wire()!r} вернула неподдерживаемый тип "
        f"{type(value).__name__} (ожидается ToolResult / str / int / float / "
        f"bool / list / tuple / set / dict / dataclass / None)"
    )
    raise TypeError(msg)


def tool_factory(
    obj: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    parse_docstring: bool = False,
) -> ToolDecoratorFactory:
    """Превратить функцию или callable-инстанс в `ToolDecoratorFactory`.

    Свободная функция-фасад вокруг `ToolDecoratorFactory.from_callable`,
    удобная когда декоратор `@tool` не подходит — например, callable-классы:

        class SearchTool:
            def __call__(self, ctx: ToolContext, query: str) -> ToolResult: ...

        factory = tool_factory(SearchTool())
        factory.into_source(SOURCE)
    """
    return ToolDecoratorFactory.from_callable(
        obj,
        name=name,
        description=description,
        parse_docstring=parse_docstring,
    )


@overload
def tool(name_or_fn: Callable[..., Any], /) -> ToolDecoratorFactory: ...


@overload
def tool(
    name_or_fn: str | None = None,
    /,
    *,
    description: str | None = None,
    parse_docstring: bool = False,
) -> Callable[[Callable[..., Any]], ToolDecoratorFactory]: ...


def tool(
    name_or_fn: Callable[..., Any] | str | None = None,
    /,
    *,
    description: str | None = None,
    parse_docstring: bool = False,
) -> ToolDecoratorFactory | Callable[[Callable[..., Any]], ToolDecoratorFactory]:
    """Превратить функцию в `ToolDecoratorFactory`.

    Формы:
        @tool                                       — bare
        @tool("custom_name")                        — переопределить имя
        @tool(description="...")                    — переопределить описание
        @tool(parse_docstring=True)                 — Google-style парсер
        @tool("custom", description="...")          — комбинация
    """
    if callable(name_or_fn) and not isinstance(name_or_fn, str):
        return ToolDecoratorFactory.from_callable(name_or_fn)

    name_override = name_or_fn

    def decorator(fn: Callable[..., Any]) -> ToolDecoratorFactory:
        return ToolDecoratorFactory.from_callable(
            fn,
            name=name_override,
            description=description,
            parse_docstring=parse_docstring,
        )

    return decorator
