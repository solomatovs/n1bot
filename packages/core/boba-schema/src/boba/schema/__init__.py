"""
Построение ObjectSchema из Python-определений

Точки входа:
- `schema_from_dataclass(cls)` — из dataclass-типа.
- `schema_from_callable(obj, ...)` — из функции или callable-инстанса.
- `build_field_from_annotation(...)` — низкоуровневый строитель одного поля
  из аннотации; используется обоими генераторами выше.
"""

from __future__ import annotations

from boba.schema.decorator import schema
from boba.schema.field import (
    Inline,
    build_field_from_annotation,
    resolve_schema_for_dataclass,
)
from boba.schema.from_callable import CallableSchema, schema_from_callable
from boba.schema.from_dataclass import schema_from_dataclass

__all__ = [
    "CallableSchema",
    "Inline",
    "build_field_from_annotation",
    "resolve_schema_for_dataclass",
    "schema",
    "schema_from_callable",
    "schema_from_dataclass",
]
