"""
Построение ObjectSchema из dataclass
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any, get_type_hints

from boba.declaration import FieldKind, ObjectSchema
from boba.schema.field import build_field_from_annotation

__all__ = ["schema_from_dataclass"]


def schema_from_dataclass(dc: type) -> ObjectSchema[Any]:
    """
    Построить ObjectSchema из определения dataclass.
    """
    if not (isinstance(dc, type) and dataclasses.is_dataclass(dc)):
        msg = f"schema_from_dataclass: ожидается dataclass-тип, получено {dc!r}"
        raise TypeError(msg)

    hints = get_type_hints(dc, include_extras=True)
    fields: list[FieldKind] = []

    for f in dataclasses.fields(dc):
        annotation = hints.get(f.name, f.type)
        default = _dataclass_field_default(f)
        fields.append(
            build_field_from_annotation(
                owner=dc.__name__,
                field_name=f.name,
                annotation=annotation,
                default=default,
                docstring_desc="",
            ),
        )

    return ObjectSchema(
        fields=fields,
        factory=dc,
        description=inspect.getdoc(dc) or "",
    )


def _dataclass_field_default(f: dataclasses.Field[Any]) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()
    return inspect.Parameter.empty
