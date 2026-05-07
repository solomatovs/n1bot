"""Предусловия наличия значения: NotNull, Required, Default, Nullable."""

from __future__ import annotations

from typing import Any

from boba.coercion.base import MISSING, Coercer, SchemaContributor, ValueCoercer
from boba.patterns import MissingValueError

__all__ = ["Default", "NotNull", "Nullable", "Required"]


class NotNull(Coercer[Any, Any]):
    """Item-level non-null: MISSING/None/NullValue → MissingValueError.

    Используется внутри `ScalarItem.coercer` для коллекций — отвергает
    null-элементы массивов/словарей. Для root-level «поле обязательно в
    объекте» используй `Required()` или `FieldSpec(..., required=True)`.
    """

    def apply(self, value: Any) -> Any:
        if value is MISSING:
            raise MissingValueError("значение отсутствует")
        if ValueCoercer.unwrap(value) is None:
            raise MissingValueError("null недопустим")
        return value


class Required(Coercer[Any, Any], SchemaContributor):
    """MISSING → MissingValueError. Симметрично с `Default`.

    В JSON-Schema эмитится через marker, который wire-builder поднимает
    в `required[]` родительского объекта (см. `REQUIRED_MARKER_KEY`).

    Альтернатива декларативному `FieldSpec(required=True)` — оба пути
    приводят к одному эффекту в wire-schema; выбирай по вкусу.
    """

    _MARKER: str = "__required__"
    """Внутренний ключ, который `Required.contribute` пишет в property-fragment.

    `ToolWireSchemaBuilder` поднимает этот marker в parent-объекта `required[]`
    и удаляет его из самого fragment'а перед эмитом наружу.
    """

    def apply(self, value: Any) -> Any:
        if value is MISSING:
            raise MissingValueError("значение отсутствует")
        return value

    def contribute(self, prop: dict[str, Any]) -> None:
        prop[self._MARKER] = True


class Default(Coercer[Any, Any], SchemaContributor):
    """Подставить default при MISSING.

    Также эмитит `default` в JSON-Schema fragment — это hint для LLM
    («что будет, если параметр опущен»).
    """

    def __init__(self, value: Any) -> None:
        self._value = value

    def apply(self, value: Any) -> Any:
        if value is MISSING:
            return self._value
        return value

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["default"] = self._value


class Nullable(Coercer[Any, Any]):
    """MISSING/None/NullValue → None; иначе делегирует во внутренний."""

    def __init__(self, inner: Coercer[Any, Any]) -> None:
        self._inner = inner

    def apply(self, value: Any) -> Any:
        if value is MISSING or ValueCoercer.unwrap(value) is None:
            return None
        return self._inner.apply(value)
