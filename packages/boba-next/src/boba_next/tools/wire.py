"""ToolWireSchemaBuilder: ObjectSchema → JSON-Schema (dict) описание Tool для LLM.

Возвращает голый `dict[str, Any]` — JSON-Schema fragment. Поле `required` берётся
из декларации `FieldSpec.required` (а не из converter'а). Конкретные converter'ы,
наследующие `SchemaContributor`, дополняют свой fragment
(`{"type":..., "minimum":..., ...}`).

Диспатч по подвидам декларации — `match`. Чистые декларации живут в
`declaration.py`; добавление нового FieldKind / ItemReader / CollectionShape
требует нового case здесь (и в `bundle.py`/`validate.py`).
"""

from __future__ import annotations

from typing import Any

from boba_next.declaration import (
    CollectionField,
    CollectionShape,
    FieldKind,
    FieldSpec,
    IndexedShape,
    ItemReader,
    KeyedShape,
    ObjectItem,
    ObjectSchema,
    ScalarItem,
)
from boba_next.validators.base import SchemaContributor

__all__ = ["ToolWireSchemaBuilder"]


class ToolWireSchemaBuilder:
    """Строит JSON-Schema (dict) описание Tool из ObjectSchema."""

    def __init__(self, schema: ObjectSchema[Any]) -> None:
        self._schema = schema

    def build(self) -> dict[str, Any]:
        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []
        for fld in self._schema.fields:
            properties[fld.name] = self._field_to_wire(fld)
            if isinstance(fld, FieldSpec) and fld.required:
                required.append(fld.name)

        out: dict[str, Any] = {"type": "object"}
        if self._schema.description:
            out["description"] = self._schema.description
        if properties:
            out["properties"] = properties
        if required:
            out["required"] = required
        return out

    def _field_to_wire(self, field: FieldKind) -> dict[str, Any]:
        match field:
            case FieldSpec(description=desc, converter=converter):
                prop: dict[str, Any] = {"description": desc}
                if isinstance(converter, SchemaContributor):
                    converter.contribute(prop)
                return prop

            case CollectionField(reader=reader, shape=shape, description=desc):
                return self._wrap_collection(
                    self._item_to_wire(reader),
                    shape,
                    description=desc,
                )

            case _:
                raise NotImplementedError(f"unknown FieldKind: {type(field).__name__}")

    def _item_to_wire(self, reader: ItemReader[Any]) -> dict[str, Any]:
        match reader:
            case ScalarItem(converter=converter):
                prop: dict[str, Any] = {}
                if isinstance(converter, SchemaContributor):
                    converter.contribute(prop)
                return prop

            case ObjectItem(schema=nested):
                # Рекурсия: вложенная схема как property "type": "object".
                return ToolWireSchemaBuilder(nested).build()

            case _:
                raise NotImplementedError(
                    f"unknown ItemReader: {type(reader).__name__}"
                )

    def _wrap_collection(
        self,
        item_wire: dict[str, Any],
        shape: CollectionShape[Any, Any, Any],
        *,
        description: str,
    ) -> dict[str, Any]:
        match shape:
            case IndexedShape():
                prop: dict[str, Any] = {
                    "type": "array",
                    "items": dict(item_wire),
                }

            case KeyedShape():
                prop = {
                    "type": "object",
                    "additionalProperties": dict(item_wire),
                }

            case _:
                raise NotImplementedError(
                    f"unknown CollectionShape: {type(shape).__name__}"
                )

        if description:
            prop["description"] = description
        return prop
