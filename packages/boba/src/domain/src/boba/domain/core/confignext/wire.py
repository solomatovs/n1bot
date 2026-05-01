"""ToolWireSchemaBuilder: ObjectSchema → JSON-Schema описание Tool для LLM.

Диспатч по подвидам декларации — `match`. Чистые декларации живут в
`declaration.py`; добавление нового FieldKind / ItemReader / CollectionShape
требует нового case здесь (и в `bundle.py`/`validate.py`).
"""

from __future__ import annotations

from typing import Any

from boba.domain.core.confignext.declaration import (
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
from boba.domain.core.confignext.schema import (
    ObjectWireSchema,
    ParamWireSchema,
    SchemaContributor,
)

__all__ = ["ToolWireSchemaBuilder"]


class ToolWireSchemaBuilder:
    """Строит JSON-Schema описание Tool из ObjectSchema (для tool-definition LLM)."""

    def __init__(self, schema: ObjectSchema[Any]) -> None:
        self._schema = schema

    def build(self) -> ObjectWireSchema:
        wire = ObjectWireSchema(description=self._schema.description)
        for fld in self._schema.fields:
            param = self._field_to_wire(fld)
            wire.properties[fld.name] = dict(param.property)
            if param.required:
                wire.required.append(fld.name)
        return wire

    def _field_to_wire(self, field: FieldKind) -> ParamWireSchema:
        match field:
            case FieldSpec(description=desc, converter=converter):
                wire = ParamWireSchema(property={"description": desc})
                if isinstance(converter, SchemaContributor):
                    converter.contribute(wire)
                return wire

            case CollectionField(reader=reader, shape=shape, description=desc):
                return self._wrap_collection(
                    self._item_to_wire(reader),
                    shape,
                    description=desc,
                )

            case _:
                raise NotImplementedError(f"unknown FieldKind: {type(field).__name__}")

    def _item_to_wire(self, reader: ItemReader[Any]) -> ParamWireSchema:
        match reader:
            case ScalarItem(converter=converter):
                wire = ParamWireSchema()
                if isinstance(converter, SchemaContributor):
                    converter.contribute(wire)
                return wire

            case ObjectItem(schema=nested):
                # Рекурсия: вложенная объектная схема как property "type": "object".
                obj = ToolWireSchemaBuilder(nested).build()
                prop: dict[str, Any] = {"type": "object"}
                if obj.description:
                    prop["description"] = obj.description
                if obj.properties:
                    prop["properties"] = obj.properties
                if obj.required:
                    prop["required"] = list(obj.required)
                return ParamWireSchema(property=prop)

            case _:
                raise NotImplementedError(
                    f"unknown ItemReader: {type(reader).__name__}"
                )

    def _wrap_collection(
        self,
        item_wire: ParamWireSchema,
        shape: CollectionShape[Any, Any, Any],
        *,
        description: str,
    ) -> ParamWireSchema:
        match shape:
            case IndexedShape():
                prop: dict[str, Any] = {
                    "type": "array",
                    "items": dict(item_wire.property),
                }

            case KeyedShape():
                prop = {
                    "type": "object",
                    "additionalProperties": dict(item_wire.property),
                }

            case _:
                raise NotImplementedError(
                    f"unknown CollectionShape: {type(shape).__name__}"
                )

        if description:
            prop["description"] = description
        return ParamWireSchema(property=prop)
