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

from boba.schema.coercion import (
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
from boba.schema.declaration import (
    CollectionField,
    FieldKind,
    FieldSpec,
    IndexedShape,
    InlineNestedField,
    KeyedShape,
    NestedField,
    ObjectItem,
    ObjectSchema,
    ScalarItem,
)

__all__ = ["Inline", "build_field_from_annotation", "resolve_schema_for_dataclass"]


@dataclasses.dataclass(frozen=True)
class Inline:
    """Маркер `Annotated[Dataclass, Inline()]` — собирать sub-DTO inline.

    Развязывает структуру DTO (композиция) и раскладку в FlatConfig
    (одна плоская секция). Автоген производит `InlineNestedField`
    вместо `NestedField`; материализатор читает дочерние поля под
    префиксом родителя, но всё равно конструирует sub-DTO. Применим
    только к полю-dataclass и несовместим с `Optional`.

    `schema` — опциональное явное переопределение схемы вложенного DTO.
    Если задано — берётся как есть; иначе схема резолвится по обычным
    правилам (ручная `ClassVar SCHEMA` на классе → автоген). Полезно во
    время миграции, когда sub-DTO имеет custom coercer-цепочку, но
    `ClassVar SCHEMA` уже снят.
    """

    schema: ObjectSchema[Any] | None = None


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

    description, annotated_coercers, inline_marker = _parse_extras(
        owner, field_name, extras,
    )
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

        nested_schema = resolve_schema_for_dataclass(inner, inline_marker)
        if inline_marker is not None:
            return InlineNestedField(
                name=field_name,
                schema=nested_schema,
                description=description,
            )
        return NestedField(
            name=field_name,
            schema=nested_schema,
            description=description,
        )

    if inline_marker is not None:
        msg = (
            f"Inline применим только к полю-dataclass "
            f"({field_name!r} в {owner!r}: тип {inner!r})"
        )
        raise TypeError(msg)

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
        return ObjectItem(schema=resolve_schema_for_dataclass(inner))

    _, annotated_coercers, inline_marker = _parse_extras(owner, field_name, extras)
    if inline_marker is not None:
        msg = (
            f"Inline неприменим к элементу коллекции "
            f"({owner!r}.{field_name!r})"
        )
        raise TypeError(msg)

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
) -> tuple[str, list[Coercer[Any, Any]], Inline | None]:
    description = ""
    annotated_coercers: list[Coercer[Any, Any]] = []
    inline_marker: Inline | None = None
    for meta in extras:
        if isinstance(meta, str):
            if not description:
                description = meta
        elif isinstance(meta, Coercer):
            annotated_coercers.append(meta)
        elif isinstance(meta, Inline):
            if inline_marker is not None:
                msg = (
                    f"повторный Inline в Annotated для "
                    f"{field_name!r} в {owner!r}"
                )
                raise TypeError(msg)
            inline_marker = meta
        else:
            msg = (
                f"неподдерживаемая Annotated-метаданная "
                f"для {field_name!r} в {owner!r}: {meta!r} "
                f"(ожидается str, Coercer или Inline)"
            )
            raise TypeError(msg)
    return description, annotated_coercers, inline_marker


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


def resolve_schema_for_dataclass(
    dc: type,
    inline_marker: Inline | None = None,
) -> ObjectSchema[Any]:
    """Подхватить явную/ручную SCHEMA с DTO, иначе сгенерировать автогеном.

    Приоритет:
      1. `Inline(schema=...)` — явное переопределение на поле родителя.
      2. `dc.__dict__["SCHEMA"]` — ручная схема, привязанная к классу
         (переходный bridge: на период миграции с ClassVar SCHEMA на
         чистый `Annotated`).
      3. Рекурсивный автоген.
    """
    if inline_marker is not None and inline_marker.schema is not None:
        return inline_marker.schema
    existing = dc.__dict__.get("SCHEMA")
    if isinstance(existing, ObjectSchema):
        return existing
    # Локальный импорт — разрываем цикл field ↔ from_dataclass.
    from boba.schema.from_dataclass import (  # noqa: PLC0415
        schema_from_dataclass,
    )

    return schema_from_dataclass(dc)
