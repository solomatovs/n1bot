"""
Построение ObjectSchema из Python-определений

Точки входа:
- `schema_from_dataclass(cls)` — из dataclass-типа.
- `schema_from_callable(obj, ...)` — из функции или callable-инстанса.
- `build_field_from_annotation(...)` — низкоуровневый строитель одного поля
  из аннотации; используется обоими генераторами выше.
"""

from __future__ import annotations

from boba.schema.field import build_field_from_annotation
from boba.schema.from_callable import CallableSchema, schema_from_callable
from boba.schema.from_dataclass import schema_from_dataclass

__all__ = [
    "CallableSchema",
    "build_field_from_annotation",
    "schema_from_callable",
    "schema_from_dataclass",
]
