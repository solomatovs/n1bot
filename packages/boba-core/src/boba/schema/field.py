"""
Преобразование аннотации параметра/поля в `FieldKind`.
"""

from __future__ import annotations

import dataclasses
import inspect
import types
from typing import (
    Any,
    Literal,
    Union,
    get_args,
    get_origin,
)

from boba.coercion import (
    ChainCoercer,
    Coercer,
    Default,
    IsBool,
    IsInt,
    IsNumber,
    IsString,
    NotNull,
    Nullable,
    OneOf,
    Required,
)
from boba.declaration import (
    CollectionField,
    FieldKind,
    FieldSpec,
    IndexedShape,
    KeyedShape,
    NestedField,
    ObjectItem,
    ScalarItem,
)

__all__ = ["build_field_from_annotation"]


_NONE_TYPE = type(None)
_DICT_TYPE_ARITY = 2

_TYPE_TO_COERCER: dict[type, Coercer[Any, Any]] = {
    str: IsString(),
    int: IsInt(),
    float: IsNumber(),
    bool: IsBool(),
}


def build_field_from_annotation(
    *,
    owner: str,
    field_name: str,
    annotation: Any,
    default: Any,
    docstring_desc: str = "",
) -> FieldKind:
    """Преобразовать аннотацию параметра/поля в `FieldKind`.

    `default` — значение по умолчанию или `inspect.Parameter.empty`, если
    его нет (поле обязательное). `docstring_desc` — fallback-описание, если
    в `Annotated`-метаданных описания нет.
    """
    inner, is_optional, extras = _normalize(annotation)

    description, annotated_coercers = _parse_extras(owner, field_name, extras)
    if not description:
        description = docstring_desc

    if _is_dataclass_type(inner):
        if annotated_coercers or is_optional:
            msg = (
                f"вложенный dataclass {inner.__name__!r} для "
                f"{field_name!r} в {owner!r}: Annotated-coercer'ы и "
                f"Optional пока не поддерживаются"
            )
            raise TypeError(msg)
        # Локальный импорт — разрываем цикл field ↔ from_dataclass.
        from boba.schema.from_dataclass import (  # noqa: PLC0415
            schema_from_dataclass,
        )

        return NestedField(
            name=field_name,
            schema=schema_from_dataclass(inner),
            description=description,
        )

    origin = get_origin(inner)
    if origin is list:
        return _build_collection(
            owner,
            field_name,
            inner,
            IndexedShape(),
            description,
        )
    if origin is dict:
        return _build_collection(
            owner,
            field_name,
            inner,
            KeyedShape(),
            description,
            is_keyed=True,
        )

    return _build_scalar(
        owner,
        field_name,
        inner,
        is_optional,
        default,
        annotated_coercers,
        description,
    )


def _build_scalar(  # noqa: PLR0913
    owner: str,
    field_name: str,
    inner: Any,
    is_optional: bool,
    default: Any,
    annotated_coercers: list[Coercer[Any, Any]],
    description: str,
) -> FieldSpec[Any]:
    coercers: list[Coercer[Any, Any]] = []
    has_default = default is not inspect.Parameter.empty

    if has_default:
        coercers.append(Default(default))
    elif is_optional:
        coercers.append(Default(None))
    else:
        coercers.append(Required())

    type_coercers = _type_coercers(owner, field_name, inner)

    if is_optional:
        coercers.append(
            Nullable(ChainCoercer(*type_coercers, *annotated_coercers)),
        )
    else:
        coercers.extend(type_coercers)
        coercers.extend(annotated_coercers)

    return FieldSpec(
        name=field_name,
        coercer=ChainCoercer(*coercers),
        description=description,
    )


def _build_collection(  # noqa: PLR0913
    owner: str,
    field_name: str,
    annotation: Any,
    shape: IndexedShape[Any] | KeyedShape[Any],
    description: str,
    *,
    is_keyed: bool = False,
) -> CollectionField[Any, Any, Any]:
    args = get_args(annotation)
    if is_keyed:
        if len(args) != _DICT_TYPE_ARITY or args[0] is not str:
            msg = (
                f"dict для {field_name!r} в {owner!r} "
                f"поддерживает только str-ключи (получено {args!r})"
            )
            raise TypeError(msg)
        elem_type = args[1]
    else:
        if len(args) != 1:
            msg = (
                f"list для {field_name!r} в {owner!r} "
                f"требует один параметр типа (получено {args!r})"
            )
            raise TypeError(msg)
        elem_type = args[0]

    reader = _build_item_reader(owner, field_name, elem_type)
    return CollectionField(
        name=field_name,
        reader=reader,
        shape=shape,
        description=description,
    )


def _build_item_reader(
    owner: str,
    field_name: str,
    elem_type: Any,
) -> ScalarItem[Any] | ObjectItem[Any]:
    inner, is_optional, extras = _normalize(elem_type)

    if is_optional:
        msg = (
            f"Optional-элементы коллекции не поддерживаются "
            f"({owner!r}.{field_name!r})"
        )
        raise TypeError(msg)

    if _is_dataclass_type(inner):
        if extras:
            msg = (
                f"Annotated-метаданные на dataclass-элементе "
                f"коллекции не поддерживаются ({owner!r}.{field_name!r})"
            )
            raise TypeError(msg)
        from boba.schema.from_dataclass import (  # noqa: PLC0415
            schema_from_dataclass,
        )

        return ObjectItem(schema=schema_from_dataclass(inner))

    _, annotated_coercers = _parse_extras(owner, field_name, extras)

    type_coercers = _type_coercers(owner, field_name, inner)
    return ScalarItem(
        coercer=ChainCoercer(NotNull(), *type_coercers, *annotated_coercers),
    )


def _type_coercers(
    owner: str,
    field_name: str,
    inner: Any,
) -> list[Coercer[Any, Any]]:
    if get_origin(inner) is Literal:
        values = get_args(inner)
        literal_types = {type(v) for v in values}
        out: list[Coercer[Any, Any]] = []
        if len(literal_types) == 1:
            guard = _TYPE_TO_COERCER.get(next(iter(literal_types)))
            if guard is not None:
                out.append(guard)
        out.append(OneOf(*values))
        return out

    guard = _TYPE_TO_COERCER.get(inner)
    if guard is None:
        msg = (
            f"неподдерживаемый тип параметра {field_name!r} "
            f"в {owner!r}: {inner!r} "
            f"(поддерживаются: str/int/float/bool, Literal[...], "
            f"T | None, list[T], dict[str, T], dataclass)"
        )
        raise TypeError(msg)
    return [guard]


def _parse_extras(
    owner: str,
    field_name: str,
    extras: tuple[Any, ...],
) -> tuple[str, list[Coercer[Any, Any]]]:
    description = ""
    annotated_coercers: list[Coercer[Any, Any]] = []
    for meta in extras:
        if isinstance(meta, str):
            if not description:
                description = meta
        elif isinstance(meta, Coercer):
            annotated_coercers.append(meta)
        else:
            msg = (
                f"неподдерживаемая Annotated-метаданная "
                f"для {field_name!r} в {owner!r}: {meta!r} "
                f"(ожидается str или Coercer)"
            )
            raise TypeError(msg)
    return description, annotated_coercers


def _normalize(annotation: Any) -> tuple[Any, bool, tuple[Any, ...]]:
    """Снять Annotated и Optional в любом порядке.

    Возвращает: (внутренний тип, is_optional, accumulated extras).
    """
    extras: tuple[Any, ...] = ()
    is_optional = False
    while True:
        if hasattr(annotation, "__metadata__"):
            extras = extras + tuple(annotation.__metadata__)
            annotation = get_args(annotation)[0]
            continue
        origin = get_origin(annotation)
        if origin is Union or origin is types.UnionType:
            args = get_args(annotation)
            non_none = tuple(a for a in args if a is not _NONE_TYPE)
            if len(non_none) == len(args):
                msg = (
                    f"Union с несколькими типами не поддерживается: "
                    f"{annotation!r}"
                )
                raise TypeError(msg)
            if len(non_none) != 1:
                msg = (
                    f"Union[..., None] с >1 не-None типом "
                    f"не поддерживается: {annotation!r}"
                )
                raise TypeError(msg)
            is_optional = True
            annotation = non_none[0]
            continue
        break
    return annotation, is_optional, extras


def _is_dataclass_type(obj: Any) -> bool:
    return isinstance(obj, type) and dataclasses.is_dataclass(obj)
