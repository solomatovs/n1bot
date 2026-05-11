"""Декораторы класса для расширения автогена `ObjectSchema`.

`@schema(invariants=...)` — приклеивает object-level coercer к dataclass.
`schema_from_dataclass` подбирает его и пробрасывает в `ObjectSchema.invariants`.
Альтернатива ClassVar-у с магическим именем: декоратор делает намерение явным
и не вводит «висящих» полей в теле класса.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any, TypeVar

from boba.schema.coercion.base import Coercer

__all__ = ["INVARIANTS_ATTR", "schema"]

INVARIANTS_ATTR = "__schema_invariants__"

T = TypeVar("T", bound=type)


def schema(
    *,
    invariants: Coercer[dict[str, Any], dict[str, Any]],
) -> Callable[[T], T]:
    """Прикрепить object-level invariants к dataclass-типу.

    Используется ПОВЕРХ `@dataclass`:

        @schema(invariants=ConfluenceConnection.invariant())
        @dataclass(frozen=True)
        class ConfluencePluginConfig:
            ...
    """

    def apply(cls: T) -> T:
        if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
            msg = (
                f"@schema применим только к dataclass-типу, "
                f"получено {cls!r}"
            )
            raise TypeError(msg)
        setattr(cls, INVARIANTS_ATTR, invariants)
        return cls

    return apply
