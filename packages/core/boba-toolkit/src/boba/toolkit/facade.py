"""Фасад инструмента без langchain: декораторы @tool и @warmup.

Модуль инструментов объявляет тело этим декоратором и живёт в песочнице на
одном pydantic: модель вызова (ToolCallBase) строится из Annotated-подписи
так же, как схему строил langchain; тело с одним параметром-наследником
ToolCallBase получает вызов этой моделью. Приложение заворачивает PayloadTool
в StructuredTool на своей стороне — payload-процесс langchain не импортирует.

Прогрев зиготы пишет автор инструмента: @warmup объявляет корутину, которая
исполняется в зиготе один раз до готовности, и её результат дети получают
форком. Хост берёт хуки из реестра WarmupHooks по имени модуля, а не ищет
условленный атрибут.

Ошибки:
ToolFacadeError — подпись тела не годится для модели вызова: нет докстринга,
    *args/**kwargs, параметр без аннотации, рядом с моделью вызова параметр
    не injected и не порт, возврат не результат семейства ToolResultBase;
    хук прогрева объявлен не корутиной либо без единственного
    параметра-модели.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Coroutine, Mapping
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

from boba.toolkit.calls import FieldMarks, ToolCallBase, ToolCallModels
from boba.toolkit.ports import StreamPorts
from boba.toolkit.result import ResultKindError, ResultKinds

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

    Распознаётся по имени класса (FieldMarks.INJECTED): сравнение типов
    между процессами невозможно.
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
    обёртка запуска (ToolProcessWrap). Тело возвращает модель результата
    (ToolResultBase); пару (content, artifact) для langchain собирает мост
    приложения, конверт ToolMain — в песочнице.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    RESPONSE_FORMAT: ClassVar[Literal["content_and_artifact"]] = "content_and_artifact"

    name: str
    description: str
    args_schema: type[ToolCallBase]
    """Модель вызова: все параметры тела, включая injected и порты."""
    call_param: str = ""
    """Параметр, которым тело принимает вызов моделью; пусто — тело берёт
    аргументы по отдельности."""
    call_class: type[ToolCallBase] | None = None
    """Класс вызова, объявленный телом; None — модель построена из подписи."""
    results: tuple[str, ...]
    """Виды результата по аннотации возврата тела; пусто — тело объявило базу."""
    func: Callable[..., Any] | None
    coroutine: Callable[..., Awaitable[Any]] | None

    def packed_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """kwargs вызова для тела: поля класса вызова собираются в его экземпляр."""
        if self.call_class is None:
            return dict(kwargs)

        own: dict[str, Any] = {}
        rest: dict[str, Any] = {}
        for name, value in kwargs.items():
            if name in self.call_class.model_fields:
                own[name] = value
            else:
                rest[name] = value

        rest[self.call_param] = self.call_class(**own)

        return rest


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

    call = _CallModel.of(fn)
    results = _results_of(fn)
    ToolCallModels.register(fn.__name__, call.model)

    if inspect.iscoroutinefunction(fn):
        return PayloadTool(
            name=fn.__name__,
            description=description,
            args_schema=call.model,
            call_param=call.param,
            call_class=call.declared,
            results=results,
            func=None,
            coroutine=fn,
        )

    return PayloadTool(
        name=fn.__name__,
        description=description,
        args_schema=call.model,
        call_param=call.param,
        call_class=call.declared,
        results=results,
        func=fn,
        coroutine=None,
    )


def _results_of(fn: Callable[..., Any]) -> tuple[str, ...]:
    """Виды результата из аннотации возврата тела."""
    annotation = get_type_hints(fn).get("return")
    if annotation is None:
        msg = (
            f"tool {fn.__name__!r} has no return annotation: the body must "
            "declare which ToolResultBase model it returns"
        )
        raise ToolFacadeError(msg)

    try:
        return ResultKinds.kinds_of(annotation)
    except ResultKindError as exc:
        msg = f"tool {fn.__name__!r}: {exc}"
        raise ToolFacadeError(msg) from exc


class _CallModel(BaseModel):
    """Модель вызова тела: построенная из подписи либо объявленная классом."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    model: type[ToolCallBase]
    param: str = ""
    declared: type[ToolCallBase] | None = None

    @classmethod
    def of(cls, fn: Callable[..., Any]) -> _CallModel:
        hints = get_type_hints(fn, include_extras=True)
        signature = inspect.signature(fn)

        banned = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)

        declared: type[ToolCallBase] | None = None
        param = ""
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
                    "annotation, the call model needs one"
                )
                raise ToolFacadeError(msg)

            bare = _bare(annotation)
            if isinstance(bare, type) and issubclass(bare, ToolCallBase):
                if declared is not None:
                    msg = (
                        f"tool {fn.__name__!r}: parameters {param!r} and {name!r} "
                        "are both call models, a body takes at most one"
                    )
                    raise ToolFacadeError(msg)

                declared = bare
                param = name
                continue

            default = parameter.default
            if default is inspect.Parameter.empty:
                default = ...

            if StreamPorts.is_port(bare):
                # порт строит гость на вызове: хост значения не передаёт, и в
                # схеме поле обязательным быть не может
                default = None

            fields[name] = (annotation, default)

        base: type[ToolCallBase] = ToolCallBase
        if declared is not None:
            cls._check_companions(fn, fields)
            base = declared

        model = create_model(f"{fn.__name__}_call", __base__=base, **fields)

        return cls(model=model, param=param, declared=declared)

    @staticmethod
    def _check_companions(fn: Callable[..., Any], fields: Mapping[str, Any]) -> None:
        """Рядом с классом вызова тело принимает только injected и порты:
        аргументы модели живут в классе."""
        probe = create_model(f"{fn.__name__}_companions", **dict(fields))
        for name, field in probe.model_fields.items():
            if FieldMarks.injected(field):
                continue

            if FieldMarks.port(field):
                continue

            msg = (
                f"tool {fn.__name__!r}: parameter {name!r} next to the call model "
                "must be injected or a port; LLM arguments belong to the model"
            )
            raise ToolFacadeError(msg)


def _bare(annotation: Any) -> Any:
    """Аннотация без Annotated-обёртки: метадату держит pydantic отдельно."""
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]

    return annotation
