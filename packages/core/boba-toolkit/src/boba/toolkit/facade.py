"""Фасад инструмента без langchain: декораторы @tool и @warmup.

Модуль инструментов объявляет тело этим декоратором и живёт в песочнице на
одном pydantic: схема аргументов строится из Annotated-подписи так же, как её
строил langchain. Приложение заворачивает PayloadTool в StructuredTool на
своей стороне — payload-процесс langchain не импортирует.

Прогрев зиготы пишет автор инструмента: @warmup объявляет корутину, которая
исполняется в зиготе один раз до готовности, и её результат дети получают
форком. Хост берёт хуки из реестра WarmupHooks по имени модуля, а не ищет
условленный атрибут.

Ошибки:
ToolFacadeError — подпись тела не годится для схемы: нет докстринга,
    *args/**kwargs, параметр без аннотации; хук прогрева объявлен не
    корутиной либо без единственного параметра-модели.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Coroutine
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    TypeAlias,
    get_args,
    get_origin,
    get_type_hints,
)

from pydantic import BaseModel, ConfigDict, create_model

from boba.toolkit.ports import StreamPorts

__all__ = [
    "Injected",
    "PayloadTool",
    "ToolFacadeError",
    "UserConnection",
    "WarmupHook",
    "WarmupHooks",
    "tool",
    "warmup",
]

WarmupBody: TypeAlias = Callable[[Any], Coroutine[Any, Any, None]]
"""Тело прогрева: одна корутина, единственный параметр — модель конфига."""


class ToolFacadeError(Exception):
    """Тело инструмента объявлено с нарушением контракта фасада."""


class Injected:
    """Маркер injected-параметра в Annotated: значение кладёт приложение.

    Распознаётся по имени класса (ToolArgv.INJECTED_MARKERS), как и
    langchain-маркеры, — сравнение типов между процессами невозможно.
    """


class UserConnection:
    """Маркер параметра-соединения: имя выбирает LLM, профиль подаёт хост.

    Тип параметра — модель профиля пакета-владельца (PostgresConfig,
    HttpConnection, ...); по ней хост узнаёт вид соединения и ищет строку среди
    выданных субъекту вызова. Модель видит на этом месте строку с именем
    соединения, тело получает готовый профиль с кредами.

    Как и Injected, распознаётся по имени класса (ToolArgv.CONNECTION_MARKERS):
    значение едет телу каналом injected, а не через argv.
    """


class PayloadTool(BaseModel):
    """Инструмент модуля: имя, описание для LLM, схема аргументов и тело.

    Реализует ToolLike структурно: наследовать протокол нельзя, метакласс
    pydantic с ним несовместим. func/coroutine — обычные поля, их подменяет
    обёртка запуска (ToolProcessWrap). Ответ тела всегда (content, artifact) —
    формат зафиксирован контрактом конверта ToolMain.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    RESPONSE_FORMAT: ClassVar[Literal["content_and_artifact"]] = "content_and_artifact"

    name: str
    description: str
    args_schema: type[BaseModel]
    func: Callable[..., Any] | None
    coroutine: Callable[..., Awaitable[Any]] | None


class WarmupHook(BaseModel):
    """Объявленный прогрев модуля: корутина и модель её конфига."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    module: str
    name: str
    body: WarmupBody
    config_model: type[BaseModel]


class WarmupHooks:
    """Реестр прогревов по модулям: наполняет @warmup, читают хост и зигота."""

    _HOOKS: ClassVar[dict[str, tuple[WarmupHook, ...]]] = {}

    @classmethod
    def add(cls, hook: WarmupHook) -> None:
        cls._HOOKS[hook.module] = (*cls._HOOKS.get(hook.module, ()), hook)

    @classmethod
    def of(cls, module: str) -> tuple[WarmupHook, ...]:
        """Прогревы модуля в порядке объявления; пусто — модуль их не имеет."""
        return cls._HOOKS.get(module, ())

    @classmethod
    def named(cls, module: str, name: str) -> WarmupHook | None:
        for hook in cls.of(module):
            if hook.name == name:
                return hook

        return None


def warmup(fn: WarmupBody) -> WarmupBody:
    """Корутина прогрева зиготы: исполняется до готовности, конфиг — параметром.

    Возвращает функцию как есть: модуль может звать её и напрямую, реестр
    нужен только хосту и зиготе.
    """
    hook = WarmupHook(
        module=fn.__module__,
        name=fn.__name__,
        body=fn,
        config_model=_warmup_config_model(fn),
    )
    WarmupHooks.add(hook)

    return fn


def _warmup_config_model(fn: WarmupBody) -> type[BaseModel]:
    """Модель конфига прогрева из аннотации единственного параметра."""
    if not inspect.iscoroutinefunction(fn):
        msg = (
            f"warmup {fn.__name__!r} must be a coroutine function "
            "(async def), got a plain callable"
        )
        raise ToolFacadeError(msg)

    parameters = list(inspect.signature(fn).parameters)
    if len(parameters) != 1:
        msg = (
            f"warmup {fn.__name__!r} must take exactly one config parameter, "
            f"got {len(parameters)}: {parameters}"
        )
        raise ToolFacadeError(msg)

    annotation = get_type_hints(fn).get(parameters[0])
    if not isinstance(annotation, type):
        msg = (
            f"warmup {fn.__name__!r}: config parameter {parameters[0]!r} "
            f"must be annotated with a pydantic model class, got {annotation!r}"
        )
        raise ToolFacadeError(msg)

    if not issubclass(annotation, BaseModel):
        msg = (
            f"warmup {fn.__name__!r}: config parameter {parameters[0]!r} "
            f"must be a pydantic model, got {annotation.__name__}"
        )
        raise ToolFacadeError(msg)

    return annotation


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
            msg = (
                f"tool {fn.__name__!r}: parameter {name!r} is {parameter.kind.name}"
                ", *args/**kwargs are not allowed in a tool signature"
            )
            raise ToolFacadeError(msg)

        annotation = hints.get(name)
        if annotation is None:
            msg = (
                f"tool {fn.__name__!r}: parameter {name!r} has no type "
                "annotation, the argument schema needs one"
            )
            raise ToolFacadeError(msg)

        default = parameter.default
        if default is inspect.Parameter.empty:
            default = ...

        if StreamPorts.is_port(_bare(annotation)):
            # порт строит гость на вызове: хост значения не передаёт, и в
            # схеме поле обязательным быть не может
            default = None

        fields[name] = (annotation, default)

    return create_model(f"{fn.__name__}_args", **fields)


def _bare(annotation: Any) -> Any:
    """Аннотация без Annotated-обёртки: метадату держит pydantic отдельно."""
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]

    return annotation
