"""ToolWireSchemaBuilder: ObjectSchema → JSON-Schema dict."""

from __future__ import annotations

from dataclasses import dataclass

from boba.coercion import ChainCoercer, Default, ParseInt, ParseString
from boba.declaration import FieldSpec, NestedField, ObjectSchema
from boba.tools.domain.wire import ToolWireSchemaBuilder


@dataclass(frozen=True)
class _Connection:
    base_url: str
    timeout_sec: int


@dataclass(frozen=True)
class _Service:
    name: str
    connection: _Connection


_CONNECTION_SCHEMA: ObjectSchema[_Connection] = ObjectSchema(
    description="Connection options.",
    fields=[
        FieldSpec(
            "base_url",
            ChainCoercer(ParseString()),
            description="Base URL.",
            required=True,
        ),
        FieldSpec(
            "timeout_sec",
            ChainCoercer(Default(30), ParseInt()),
            description="Timeout in seconds.",
        ),
    ],
    factory=_Connection,
)


_SERVICE_SCHEMA: ObjectSchema[_Service] = ObjectSchema(
    description="Service config.",
    fields=[
        FieldSpec("name", ChainCoercer(ParseString()), description="Service name."),
        NestedField(
            name="connection",
            schema=_CONNECTION_SCHEMA,
            description="Outbound connection settings.",
        ),
    ],
    factory=_Service,
)


def test_wire_nested_field_builds_inner_object():
    wire = ToolWireSchemaBuilder(_SERVICE_SCHEMA).build()
    assert wire["type"] == "object"
    assert wire["description"] == "Service config."
    nested = wire["properties"]["connection"]
    assert nested["type"] == "object"
    assert nested["description"] == "Outbound connection settings."
    assert nested["properties"]["base_url"]["description"] == "Base URL."
    assert nested["required"] == ["base_url"]


def test_wire_nested_field_falls_back_to_inner_description_when_outer_empty():
    schema: ObjectSchema[_Service] = ObjectSchema(
        fields=[
            FieldSpec("name", ChainCoercer(ParseString())),
            NestedField(name="connection", schema=_CONNECTION_SCHEMA),
        ],
        factory=_Service,
    )
    wire = ToolWireSchemaBuilder(schema).build()
    nested = wire["properties"]["connection"]
    assert nested["description"] == "Connection options."
