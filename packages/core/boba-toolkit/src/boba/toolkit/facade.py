"""Фасад инструмента без langchain: декоратор @tool и модель PayloadTool.

Модуль инструментов объявляет тело этим декоратором и живёт в песочнице на
одном pydantic: схема аргументов строится из Annotated-подписи так же, как её
строил langchain. Приложение заворачивает PayloadTool в StructuredTool на
своей стороне — payload-процесс langchain не импортирует.

Ошибки:
ToolFacadeError — подпись тела не годится для схемы: нет докстринга,
    *args/**kwargs, параметр без аннотации.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, Literal, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model

__all__ = ["Injected", "PayloadTool", "ToolFacadeError", "tool"]


class ToolFacadeError(Exception):
    """Тело инструмента объявлено с нарушением контракта фасада."""


class Injected:
    """Маркер injected-параметра в Annotated: значение кладёт приложение.

    Распознаётся по имени класса (ToolArgv.INJECTED_MARKERS), как и
    langchain-маркеры, — сравнение типов между процессами невозможно.
    """


class PayloadTool(BaseModel):
    """Инструмент модуля: имя, описание для LLM, схема аргументов и тело.

    Реализует ToolLike; func/coroutine — обычные поля, их подменяет обёртка
    запуска (ToolProcessWrap). Ответ тела всегда (content, artifact) — формат
    зафиксирован контрактом конверта ToolMain.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    RESPONSE_FORMAT: ClassVar[Literal["content_and_artifact"]] = (
        "content_and_artifact"
    )

    name: str
    description: str
    args_schema: type[BaseModel]
    func: Callable[..., Any] | None
    coroutine: Callable[..., Awaitable[Any]] | None


def tool(fn: Callable[..., Any]) -> PayloadTool:
    """Тело инструмента -> PayloadTool: схема из подписи, описание из докстринга."""
    description = inspect.getdoc(fn)
    if not description:
        msg = f"tool {fn.__name__!r} has no docstring: LLM needs a description"
        raise ToolFacadeError(msg)

    schema = _schema_of(fn)

    if inspect.iscoroutinefunction(fn):
        return PayloadTool(
            name=fn.__name__,
            description=description,
            args_schema=schema,
            func=None,
            coroutine=fn,
        )

    return PayloadTool(
        name=fn.__name__,
        description=description,
        args_schema=schema,
        func=fn,
        coroutine=None,
    )


def _schema_of(fn: Callable[..., Any]) -> type[BaseModel]:
    """Pydantic-модель аргументов из Annotated-подписи тела."""
    hints = get_type_hints(fn, include_extras=True)
    signature = inspect.signature(fn)

    banned = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)

    fields: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in banned:
            msg = f"tool {fn.__name__!r}: *args/**kwargs are not allowed"
            raise ToolFacadeError(msg)

        annotation = hints.get(name)
        if annotation is None:
            msg = f"tool {fn.__name__!r}: parameter {name!r} has no annotation"
            raise ToolFacadeError(msg)

        default = parameter.default
        if default is inspect.Parameter.empty:
            default = ...

        fields[name] = (annotation, default)

    return create_model(f"{fn.__name__}_args", **fields)
