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
    JsonResult,
    TextResult,
    ToolResult,
    ToolResultBase,
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
            description=self._description,
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

    args_model — pydantic-модель, сгенерированная `callable_to_args_model`.
    Tool ABC использует её через стандартный pydantic-путь.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        tool_id: ToolId,
        args_model: type[BaseModel],
        description: str,
        injects_ctx: bool,
    ) -> None:
        self._fn = fn
        self._tool_id_value = tool_id
        self._args_model_value = args_model
        self._description = description
        self._injects_ctx = injects_ctx
        # Tool.__init__ ставит _cfg/_ctx/_source_id; для @tool их нет.
        source_id, _name = parse_tool_id(tool_id)
        self._cfg = None  # type: ignore[assignment]
        self._ctx = None
        self._source_id = source_id

    def tool_id(self) -> ToolId:
        return self._tool_id_value

    @classmethod
    def _try_resolve_args_type(cls) -> type | None:
        """`DecoratedTool` хранит args_model на инстансе, не в generic."""
        return None

    def definition(self) -> dict[str, Any]:
        from boba.tools.domain.llm_schema import clean_llm_json_schema  # noqa: PLC0415

        raw = self._args_model_value.model_json_schema()
        if self._description:
            raw["description"] = self._description
        return clean_llm_json_schema(raw)

    def _parse_args(self, raw: dict[str, Any]) -> Any:
        return self._parse_pydantic(self._args_model_value, raw)

    def execute(self, ctx: ToolContext, args: BaseModel) -> ToolResult:
        # getattr вместо model_dump(): не сериализует nested BaseModel
        # в dict'ы — функция получит typed-инстансы.
        kwargs = {
            name: getattr(args, name)
            for name in self._args_model_value.model_fields
        }
        raw = self._fn(ctx, **kwargs) if self._injects_ctx else self._fn(**kwargs)
        return _coerce_result(self._tool_id_value, raw)


_ResultRule = tuple[Callable[[Any], bool], Callable[[Any], ToolResult]]

_RESULT_COERCERS: tuple[_ResultRule, ...] = (
    (lambda v: v is None, lambda _: TextResult(text="null")),
    (lambda v: isinstance(v, ToolResultBase), lambda v: v),
    (lambda v: isinstance(v, str), lambda v: TextResult(text=v)),
    (lambda v: isinstance(v, (bool, int, float)), lambda v: TextResult(text=str(v))),
    (
        lambda v: isinstance(v, BaseModel),
        lambda v: JsonResult(payload=v.model_dump()),
    ),
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
        f"@tool: функция {tool_id!r} вернула неподдерживаемый тип "
        f"{type(value).__name__} (ожидается ToolResult / str / int / float / "
        f"bool / list / tuple / set / dict / BaseModel / dataclass / None)"
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
