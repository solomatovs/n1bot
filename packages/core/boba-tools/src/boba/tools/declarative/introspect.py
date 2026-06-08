"""
Введение callable'а в CallPlan: pydantic args model + DI plan.

CallPlan — кешируемый результат разбора сигнатуры tool'а или provider'а.
Делается **один раз** на регистрации plugin'а, дальше runtime invoke
использует кешированный план без повторного inspect'а.

Разделение параметров:
- Annotated[T, FromDI(...)] -> DI-dep
- Annotated[T, FromConfig(...)] -> DI-dep (но с особой semantic'ой
  «авто-загрузи cfg» на этапе сборки контейнера — это резолвится в
  container.py, тут просто различаем тип маркера)
- Всё остальное -> LLM-arg (попадает в pydantic args model)
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, create_model

from boba.tools.declarative.errors import ToolDeclarationError
from boba.tools.declarative.inject import FromConfig, FromDI

__all__ = [
    "CallPlan",
    "DiDep",
    "build_call_plan",
]


@dataclass(frozen=True)
class DiDep:
    """Одна DI-зависимость tool'а или provider'а.

    param_name — имя kwarg'а в callable.
    target_type — тип, который надо резолвить из контейнера.
    marker — FromDI(scope=...) или FromConfig().
        Различие в семантике регистрации (FromConfig авто-загружается,
        FromDI ищется среди registered providers'ов), резолюция через
        container.get(T) одинаковая.
    """

    param_name: str
    target_type: type
    marker: FromDI | FromConfig


@dataclass(frozen=True)
class CallPlan:
    """
    Разобранная сигнатура callable'а — план для runtime invoke.
    """

    description: str
    args_model: type[BaseModel]
    di_deps: tuple[DiDep, ...]
    return_type: Any

    def lacks_return_type(self) -> bool:
        """True если return-аннотация отсутствует (empty/None).

        Для provider'а return-тип обязателен — это тип, под которым служба
        регистрируется в DI; ToolBuilder.register_provider использует это
        для отказа в регистрации.
        """
        return self.return_type is inspect.Parameter.empty or self.return_type is None


def build_call_plan(obj: Any) -> CallPlan:
    """
    создать план выполнения объекта (только разбор сигнатуры, без имени)

    Поддерживает:
    - функции: def my_tool(...) -> T:
    - classes: class MyTool: def __call__(self, ...) -> T: — обходится __call__
    - callable-инстансы: MyTool() — обходится type(obj).__call__
    """
    meta = _resolve_callable_meta(obj)

    fields: dict[str, Any] = {}
    di_deps: list[DiDep] = []

    for param in meta.params:
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            msg = (
                f"{meta.name!r}: *args/**kwargs не поддерживаются "
                f"(параметр {param.name!r})"
            )
            raise ToolDeclarationError(msg)

        annotation = meta.hints.get(param.name, param.annotation)
        if annotation is inspect.Parameter.empty:
            msg = (
                f"{meta.name!r}: параметр {param.name!r} без аннотации; "
                f"укажи тип явно (LLM-args требуют тип для JSON schema, "
                f"DI-deps — для container.get(T))"
            )
            raise ToolDeclarationError(msg)

        marker = _extract_inject_marker(annotation)
        if marker is not None:
            target_type = _strip_annotated(annotation)
            di_deps.append(
                DiDep(
                    param_name=param.name,
                    target_type=target_type,
                    marker=marker,
                ),
            )
        else:
            fields[param.name] = _build_pydantic_field(annotation, param.default)

    args_model = create_model(
        f"{meta.name}__Args",
        __config__=ConfigDict(frozen=True, extra="forbid"),
        __doc__=meta.docstring,
        **fields,
    )
    return CallPlan(
        description=meta.docstring,
        args_model=args_model,
        di_deps=tuple(di_deps),
        return_type=meta.return_type,
    )


@dataclass(frozen=True)
class CallableMeta:
    raw_name: str
    name: str
    docstring: str
    params: list[inspect.Parameter]
    hints: dict[str, Any]
    return_type: Any


def _resolve_callable_meta(obj: Any) -> CallableMeta:
    """Разобрать объект и извлечь метаданные для построения CallPlan."""
    # Поддерживаем функции, классы и callable-инстансы.
    # Для классов и инстансов используем __call__,
    # но имя и докстринг берём из самого объекта, а не из __call__.
    if inspect.isfunction(obj) or inspect.ismethod(obj):
        raw_name = obj.__name__
        sig_target = obj
        hint_target = obj
        skip_self = False

    # Если это класс, то предполагаем, что он — инструмент
    elif inspect.isclass(obj):
        raw_name = obj.__name__
        # __call__ говорит о том, что класс можно вызвать
        if not callable(obj):
            msg = f"{raw_name}: класс должен иметь `__call__`"
            raise ToolDeclarationError(msg)

        sig_target = obj.__call__
        hint_target = obj.__call__
        # у классов первым аргументом будет self
        skip_self = True
    # для callable-инстансов используем type(obj).__call__
    elif callable(obj):
        raw_name = type(obj).__name__
        sig_target = type(obj).__call__
        hint_target = type(obj).__call__
        skip_self = True
    else:
        msg = f"introspect_callable: {obj!r} не callable"
        raise ToolDeclarationError(msg)

    sig = inspect.signature(sig_target)
    params = list(sig.parameters.values())
    if skip_self and params and params[0].name == "self":
        params = params[1:]

    hints = get_type_hints(hint_target, include_extras=True)

    return_type = _unwrap_generator_return(
        hints.get("return", inspect.Parameter.empty),
    )

    return CallableMeta(
        raw_name=raw_name,
        name=raw_name,
        docstring=inspect.getdoc(obj) or "",
        params=params,
        hints=hints,
        return_type=return_type,
    )


def _unwrap_generator_return(rt: Any) -> Any:
    """Iterator[T] / Generator[T, ...] -> T; иначе — без изменений.

    Generator-providers объявляют return как Iterator[T], чтобы фреймворк
    (и Dishka) поняли, что нужно сделать yield + cleanup. Но «provided
    тип» — это T, не Iterator[T]. Унификация: framework везде работает
    с T, а сам generator-факт Dishka детектит по yield в теле функции.
    """
    if rt is inspect.Parameter.empty or rt is None:
        return rt
    origin = get_origin(rt)
    if origin is None:
        return rt
    from collections.abc import (  # noqa: PLC0415
        AsyncGenerator,
        AsyncIterator,
        Generator,
        Iterator,
    )

    if origin in (Iterator, Generator, AsyncIterator, AsyncGenerator):
        args = get_args(rt)
        if args:
            return args[0]

    return rt


def _extract_inject_marker(annotation: Any) -> FromDI | FromConfig | None:
    """
    Найти FromDI/FromConfig в Annotated-метаданных.

    Возвращает первый встреченный маркер

    Если в метаданных два маркера - это ошибка.
    """
    if get_origin(annotation) is not Annotated:
        return None

    _, *metadata = get_args(annotation)
    found: list[FromDI | FromConfig] = [
        m for m in metadata if isinstance(m, (FromDI, FromConfig))
    ]

    if not found:
        return None

    if len(found) > 1:
        msg = (
            f"параметр имеет несколько inject-маркеров "
            f"({[type(m).__name__ for m in found]}); должен быть один"
        )
        raise ToolDeclarationError(msg)

    return found[0]


def _strip_annotated(annotation: Any) -> type:
    """
    Annotated[T, ...] -> T
    Для не-Annotated — возвращает как есть
    """
    if get_origin(annotation) is Annotated:
        return get_args(annotation)[0]

    return annotation


def _build_pydantic_field(annotation: Any, default: Any) -> tuple[Any, Any]:
    """
    Пара (annotation, default|Field) для create_model.

    Если в Annotated[T, "str", ...] есть голая строка
    это shortcut для Field(description=...)

    Иначе аннотация передаётся в pydantic как есть
    """
    has_default = default is not inspect.Parameter.empty
    description: str | None = None

    if get_origin(annotation) is Annotated:
        type_arg, *meta = get_args(annotation)
        kept_meta: list[Any] = []
        for m in meta:
            if isinstance(m, str) and description is None:
                description = m
                continue
            kept_meta.append(m)
        if description is not None:
            # Реконструируем Annotated без голой строки, добавив Field.
            field = Field(
                default=default if has_default else ...,
                description=description,
            )
            if kept_meta:
                annotation = Annotated[(type_arg, *kept_meta, field)]
                return (annotation, default if has_default else ...)
            return (type_arg, field)

    return (annotation, default if has_default else ...)
