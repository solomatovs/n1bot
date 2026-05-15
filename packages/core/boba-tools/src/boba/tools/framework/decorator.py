"""Декоратор `@tool` и фабрика `tool_factory`: callable → `ToolDecoratorFactory`.

Тонкий слой поверх `callable_to_args_model`: добавляет ToolContext-инжекцию,
ToolId/ToolName и обработку возвращаемого значения функции.

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
- pydantic `BaseModel` — `JsonResult(payload=value.model_dump())`
- dataclass-инстанс — `JsonResult(payload=dataclasses.asdict(value))`
- `None` — `TextResult(text="null")`
- прочее — `TypeError`
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, overload

from pydantic import BaseModel

from boba.tools.domain.ids import (
    ToolId,
    ToolName,
    ToolSourceId,
    compose_tool_id,
    parse_tool_id,
)
from boba.tools.domain.result import (
    ErrorResult,
    JsonResult,
    TextResult,
    ToolResult,
)
from boba.tools.domain.tool import Tool, ToolContext
from boba.tools.framework.from_callable import callable_to_args_model
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
        args_model: type[BaseModel],
        injects_ctx: bool,
    ) -> None:
        self._fn = fn
        self._name = name
        self._description = description
        self._args_model = args_model
        self._injects_ctx = injects_ctx

    @property
    def name(self) -> ToolName:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def args_model(self) -> type[BaseModel]:
        return self._args_model

    @property
    def injects_ctx(self) -> bool:
        return self._injects_ctx

    def build(self, source_id: ToolSourceId) -> Tool[BaseModel, None]:
        """Привязать к source_id и вернуть готовый Tool."""
        return DecoratedTool(
            fn=self._fn,
            tool_id=compose_tool_id(source_id, self._name),
            args_model=self._args_model,
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
        parsed = callable_to_args_model(
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

        # Описание попадёт в JSON-schema как корневой `description`
        # стандартным pydantic-путём через class docstring.
        parsed.args_model.__doc__ = description_final or None

        return cls(
            fn=obj,
            name=ToolName(name_final),
            description=description_final,
            args_model=parsed.args_model,
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


class DecoratedTool(Tool[BaseModel, None]):
    """Concrete Tool, рождённый @tool. tool_id заполнен через ToolFactory.build.

    args_model — pydantic-модель, сгенерированная `callable_to_args_model`,
    с уже проставленным `__doc__` (описание tool'а попадёт в JSON-schema
    стандартным pydantic-путём). Tool ABC рулит всем остальным.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        tool_id: ToolId,
        args_model: type[BaseModel],
        injects_ctx: bool,
    ) -> None:
        self._fn = fn
        self._tool_id_value = tool_id
        self._args_model_value = args_model
        self._injects_ctx = injects_ctx
        # Tool.__init__ ставит _cfg/_ctx/_source_id; для @tool их нет.
        source_id, _name = parse_tool_id(tool_id)
        self._cfg = None  # type: ignore[assignment]
        self._ctx = None
        self._source_id = source_id

    def tool_id(self) -> ToolId:
        return self._tool_id_value

    def _args_model_class(self) -> type[BaseModel]:
        """`@tool` строит модель через `create_model`; generic не задействован."""
        return self._args_model_value

    def execute(self, ctx: ToolContext, args: BaseModel) -> ToolResult:
        # getattr вместо model_dump(): не сериализует nested BaseModel
        # в dict'ы — функция получит typed-инстансы.
        kwargs = {
            name: getattr(args, name)
            for name in self._args_model_value.model_fields
        }
        raw = self._fn(ctx, **kwargs) if self._injects_ctx else self._fn(**kwargs)
        return self._tools_result(self._tool_id_value, raw)


    @staticmethod
    def _tools_result(tool_id: ToolId, value: Any) -> ToolResult:  # noqa: PLR0911
        """Привести возврат функции к ToolResult.

        Порядок важен: готовый `ToolResult` (TextResult/JsonResult/ErrorResult)
        пробрасывается as-is до общих типов; `str`/`bool|int|float` — до
        `BaseModel`/`dataclass`/`dict`; `dataclass` — guard'ом, потому что это
        маркер, а не тип-предок.
        """
        match value:
            case None:
                return TextResult(text="null")
            case TextResult() | JsonResult() | ErrorResult():
                return value
            case str():
                return TextResult(text=value)
            case bool() | int() | float():
                return TextResult(text=str(value))
            case BaseModel():
                return JsonResult(payload=value.model_dump())
            case _ if dataclasses.is_dataclass(value) and not isinstance(value, type):
                return JsonResult(payload=dataclasses.asdict(value))
            case dict():
                return JsonResult(payload=value)
            case list() | tuple() | set() | frozenset():
                return JsonResult(payload=list(value))
            case _:
                msg = (
                    f"@tool: функция {tool_id!r} вернула неподдерживаемый тип "
                    f"{type(value).__name__} (ожидается ToolResult / str / int / "
                    f"float / bool / list / tuple / set / dict / BaseModel / "
                    f"dataclass / None)"
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
