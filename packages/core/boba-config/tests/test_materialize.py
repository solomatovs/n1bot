"""materialize: ObjectSchema + FieldSpec / CollectionField → DTO."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from boba.config.bundle import ConfigBundle, FlatConfigMaterializer
from boba.config.path import ConfigPath, NameSegment
from boba.config.source.dict import DictSource
from boba.schema.coercion import (
    ChainCoercer,
    Default,
    MinValue,
    NonEmpty,
    ParseBool,
    ParseInt,
    ParseString,
    Required,
)
from boba.schema.declaration import (
    CollectionField,
    FieldPathError,
    FieldPathMissingError,
    FieldSpec,
    IndexedShape,
    InlineNestedField,
    KeyedShape,
    NestedField,
    ObjectItem,
    ObjectSchema,
)
from boba.schema.value import (
    BoolValue,
    IntValue,
    StringValue,
)


@dataclass(frozen=True)
class _Agent:
    max_iterations: int
    enabled: bool


_AGENT_SCHEMA: ObjectSchema[_Agent] = ObjectSchema(
    fields=[
        FieldSpec(
            name="max_iterations",
            coercer=ChainCoercer(Default(20), ParseInt(), MinValue(1)),
        ),
        FieldSpec(
            name="enabled",
            coercer=ChainCoercer(Default(False), ParseBool()),
        ),
    ],
    factory=_Agent,
)


def test_scalar_with_defaults():
    flat = ConfigBundle.from_sources([DictSource({})]).flat
    agent = FlatConfigMaterializer(_AGENT_SCHEMA).materialize(
        flat, ConfigPath.parse("agent")
    )
    assert agent == _Agent(max_iterations=20, enabled=False)


def test_scalar_overridden():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("agent.max_iterations"): IntValue(200),
                    ConfigPath.parse("agent.enabled"): BoolValue(True),
                }
            )
        ]
    ).flat
    agent = FlatConfigMaterializer(_AGENT_SCHEMA).materialize(
        flat, ConfigPath.parse("agent")
    )
    assert agent == _Agent(max_iterations=200, enabled=True)


def test_required_missing_raises():
    schema: ObjectSchema[dict] = ObjectSchema(
        fields=[FieldSpec("x", ChainCoercer(Required(), ParseString()), )],
    )
    flat = ConfigBundle.from_sources([DictSource({})]).flat
    with pytest.raises(FieldPathMissingError):
        FlatConfigMaterializer(schema).materialize(flat, ConfigPath.parse("root"))


def test_validation_error_attaches_field():
    schema: ObjectSchema[dict] = ObjectSchema(
        fields=[
            FieldSpec(
                "max_iterations",
                ChainCoercer(Required(), ParseInt(), MinValue(1)),
            ),
        ],
    )
    flat = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("root.max_iterations"): IntValue(0)})]
    ).flat
    with pytest.raises(FieldPathError) as info:
        FlatConfigMaterializer(schema).materialize(flat, ConfigPath.parse("root"))
    assert info.value.field_name == "max_iterations"




@dataclass(frozen=True)
class _ToolEntry:
    enabled: bool
    description: str


_TOOL_ENTRY_SCHEMA: ObjectSchema[_ToolEntry] = ObjectSchema(
    fields=[
        FieldSpec("enabled", ChainCoercer(Default(False), ParseBool())),
        FieldSpec("description", ChainCoercer(Default(""), ParseString())),
    ],
    factory=_ToolEntry,
)


@dataclass(frozen=True)
class _ExtBlock:
    enabled: bool
    tools: Mapping[str, _ToolEntry]


_EXT_SCHEMA: ObjectSchema[_ExtBlock] = ObjectSchema(
    fields=[
        FieldSpec("enabled", ChainCoercer(Default(False), ParseBool())),
        CollectionField(
            name="tools",
            reader=ObjectItem(_TOOL_ENTRY_SCHEMA),
            shape=KeyedShape(),
        ),
    ],
    factory=_ExtBlock,
)


def test_mapping_field_collects_dynamic_subsections():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("ext.html.enabled"): BoolValue(True),
                    ConfigPath.parse("ext.html.tools.html_outline.enabled"): BoolValue(
                        True
                    ),
                    ConfigPath.parse(
                        "ext.html.tools.html_outline.description"
                    ): StringValue("custom outline"),
                    ConfigPath.parse(
                        "ext.html.tools.html_section.description"
                    ): StringValue("custom section"),
                }
            )
        ]
    ).flat
    block = FlatConfigMaterializer(_EXT_SCHEMA).materialize(
        flat, ConfigPath.parse("ext.html")
    )
    assert block.enabled is True
    assert set(block.tools) == {"html_outline", "html_section"}
    assert block.tools["html_outline"].enabled is True
    assert block.tools["html_outline"].description == "custom outline"
    assert block.tools["html_section"].enabled is False  # default
    assert block.tools["html_section"].description == "custom section"


def test_mapping_field_empty_when_no_subsections():
    flat = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("ext.html.enabled"): BoolValue(True)})]
    ).flat
    block = FlatConfigMaterializer(_EXT_SCHEMA).materialize(
        flat, ConfigPath.parse("ext.html")
    )
    assert block.tools == {}




@dataclass(frozen=True)
class _Model:
    name: str


_MODEL_SCHEMA: ObjectSchema[_Model] = ObjectSchema(
    fields=[
        FieldSpec("name", ChainCoercer(Required(), ParseString(), NonEmpty()), )
    ],
    factory=_Model,
)


@dataclass(frozen=True)
class _ChainlitBlock:
    models: tuple[_Model, ...]


_CHAINLIT_SCHEMA: ObjectSchema[_ChainlitBlock] = ObjectSchema(
    fields=[
        CollectionField(
            name="models",
            reader=ObjectItem(_MODEL_SCHEMA),
            shape=IndexedShape(),
        ),
    ],
    factory=_ChainlitBlock,
)


def test_list_field_collects_indexed_subsections():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("chainlit.models[0].name"): StringValue("qwen3"),
                    ConfigPath.parse("chainlit.models[1].name"): StringValue("gemini"),
                    ConfigPath.parse("chainlit.models[2].name"): StringValue(
                        "deepseek"
                    ),
                }
            )
        ]
    ).flat
    block = FlatConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
        flat, ConfigPath.parse("chainlit")
    )
    assert tuple(m.name for m in block.models) == ("qwen3", "gemini", "deepseek")


def test_list_field_uses_segment_indices_in_sorted_order():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("chainlit.models[2].name"): StringValue("c"),
                    ConfigPath.parse("chainlit.models[0].name"): StringValue("a"),
                    ConfigPath.parse("chainlit.models[1].name"): StringValue("b"),
                }
            )
        ]
    ).flat
    block = FlatConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
        flat, ConfigPath.parse("chainlit")
    )
    assert tuple(m.name for m in block.models) == ("a", "b", "c")


def test_nested_field_error_propagates_with_location():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("ext.html.tools.html_outline.enabled"): IntValue(
                        42
                    )  # bool ожидался
                }
            )
        ]
    ).flat
    with pytest.raises(FieldPathError) as info:
        FlatConfigMaterializer(_EXT_SCHEMA).materialize(
            flat, ConfigPath.parse("ext.html")
        )
    assert "tools" in info.value.field_name or "tools" in str(info.value)
    assert "html_outline" in str(info.value)


# --- NestedField ---


@dataclass(frozen=True)
class _Connection:
    base_url: str
    timeout_sec: int


@dataclass(frozen=True)
class _Service:
    name: str
    connection: _Connection


_CONNECTION_SCHEMA: ObjectSchema[_Connection] = ObjectSchema(
    fields=[
        FieldSpec(
            name="base_url",
            coercer=ChainCoercer(Required(), ParseString()),
        ),
        FieldSpec(
            name="timeout_sec",
            coercer=ChainCoercer(Default(30), ParseInt(), MinValue(1)),
        ),
    ],
    factory=_Connection,
)


_SERVICE_SCHEMA: ObjectSchema[_Service] = ObjectSchema(
    fields=[
        FieldSpec(name="name", coercer=ChainCoercer(Default("svc"), ParseString())),
        NestedField(name="connection", schema=_CONNECTION_SCHEMA),
    ],
    factory=_Service,
)


def test_nested_field_materializes_inner_object():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("svc.name"): StringValue("api"),
                    ConfigPath.parse("svc.connection.base_url"): StringValue("https://example.com"),
                    ConfigPath.parse("svc.connection.timeout_sec"): IntValue(60),
                }
            )
        ]
    ).flat
    svc = FlatConfigMaterializer(_SERVICE_SCHEMA).materialize(
        flat, ConfigPath.parse("svc")
    )
    assert svc == _Service(
        name="api",
        connection=_Connection(base_url="https://example.com", timeout_sec=60),
    )


def test_nested_field_uses_defaults_for_missing_inner():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("svc.connection.base_url"): StringValue("https://example.com"),
                }
            )
        ]
    ).flat
    svc = FlatConfigMaterializer(_SERVICE_SCHEMA).materialize(
        flat, ConfigPath.parse("svc")
    )
    assert svc == _Service(
        name="svc",
        connection=_Connection(base_url="https://example.com", timeout_sec=30),
    )


def test_nested_field_propagates_required_error():
    flat = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("svc.name"): StringValue("api")})]
    ).flat
    with pytest.raises(FieldPathMissingError):
        FlatConfigMaterializer(_SERVICE_SCHEMA).materialize(
            flat, ConfigPath.parse("svc")
        )


def test_nested_field_two_levels_deep():
    @dataclass(frozen=True)
    class _Outer:
        svc: _Service

    outer_schema: ObjectSchema[_Outer] = ObjectSchema(
        fields=[NestedField(name="svc", schema=_SERVICE_SCHEMA)],
        factory=_Outer,
    )

    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("root.svc.name"): StringValue("api"),
                    ConfigPath.parse("root.svc.connection.base_url"): StringValue(
                        "https://example.com"
                    ),
                }
            )
        ]
    ).flat
    outer = FlatConfigMaterializer(outer_schema).materialize(
        flat, ConfigPath.parse("root")
    )
    assert outer == _Outer(
        svc=_Service(
            name="api",
            connection=_Connection(base_url="https://example.com", timeout_sec=30),
        ),
    )


# --- InlineNestedField ---


@dataclass(frozen=True)
class _InlineOuter:
    name: str
    conn: _Connection


_INLINE_SCHEMA: ObjectSchema[_InlineOuter] = ObjectSchema(
    fields=[
        FieldSpec(name="name", coercer=ChainCoercer(Default("svc"), ParseString())),
        InlineNestedField(name="conn", schema=_CONNECTION_SCHEMA),
    ],
    factory=_InlineOuter,
)


def test_inline_nested_reads_children_under_parent_prefix():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("svc.name"): StringValue("api"),
                    ConfigPath.parse("svc.base_url"): StringValue("https://example.com"),
                    ConfigPath.parse("svc.timeout_sec"): IntValue(60),
                }
            )
        ]
    ).flat
    outer = FlatConfigMaterializer(_INLINE_SCHEMA).materialize(
        flat, ConfigPath.parse("svc")
    )
    assert outer == _InlineOuter(
        name="api",
        conn=_Connection(base_url="https://example.com", timeout_sec=60),
    )


def test_inline_nested_uses_inner_defaults():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {ConfigPath.parse("svc.base_url"): StringValue("https://example.com")}
            )
        ]
    ).flat
    outer = FlatConfigMaterializer(_INLINE_SCHEMA).materialize(
        flat, ConfigPath.parse("svc")
    )
    assert outer == _InlineOuter(
        name="svc",
        conn=_Connection(base_url="https://example.com", timeout_sec=30),
    )


def test_inline_nested_propagates_required_error():
    flat = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("svc.name"): StringValue("api")})]
    ).flat
    with pytest.raises(FieldPathMissingError):
        FlatConfigMaterializer(_INLINE_SCHEMA).materialize(
            flat, ConfigPath.parse("svc")
        )


def test_inline_nested_does_not_conflict_with_sibling_inline():
    """Два inline-DTO рядом — оба читают из одного префикса без конфликта."""

    @dataclass(frozen=True)
    class _Lhs:
        a: str

    @dataclass(frozen=True)
    class _Rhs:
        b: str

    @dataclass(frozen=True)
    class _Pair:
        lhs: _Lhs
        rhs: _Rhs

    lhs_schema: ObjectSchema[_Lhs] = ObjectSchema(
        fields=[FieldSpec(name="a", coercer=ChainCoercer(Required(), ParseString()))],
        factory=_Lhs,
    )
    rhs_schema: ObjectSchema[_Rhs] = ObjectSchema(
        fields=[FieldSpec(name="b", coercer=ChainCoercer(Required(), ParseString()))],
        factory=_Rhs,
    )
    pair_schema: ObjectSchema[_Pair] = ObjectSchema(
        fields=[
            InlineNestedField(name="lhs", schema=lhs_schema),
            InlineNestedField(name="rhs", schema=rhs_schema),
        ],
        factory=_Pair,
    )
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("root.a"): StringValue("AA"),
                    ConfigPath.parse("root.b"): StringValue("BB"),
                }
            )
        ]
    ).flat
    pair = FlatConfigMaterializer(pair_schema).materialize(
        flat, ConfigPath.parse("root")
    )
    assert pair == _Pair(lhs=_Lhs(a="AA"), rhs=_Rhs(b="BB"))


# --- ConfigBundle.get: диспатч DTO/ObjectSchema ---


@dataclass(frozen=True)
class _DispatchDTO:
    """DTO для теста диспатча: без ClassVar SCHEMA."""

    name: str = "default"
    port: int = 8080


def test_get_accepts_dataclass_and_autogens_schema():
    """bundle.get(DTO, prefix) — без ClassVar SCHEMA автоген отрабатывает."""
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("svc.name"): StringValue("api"),
                    ConfigPath.parse("svc.port"): IntValue(9000),
                }
            )
        ]
    )
    got = flat.get(_DispatchDTO, "svc")
    assert got == _DispatchDTO(name="api", port=9000)


def test_get_accepts_explicit_object_schema():
    """bundle.get(ObjectSchema, prefix) — schema используется как есть."""
    schema: ObjectSchema[_DispatchDTO] = ObjectSchema(
        fields=[
            FieldSpec(
                name="name",
                coercer=ChainCoercer(Default("manual"), ParseString()),
            ),
            FieldSpec(
                name="port",
                coercer=ChainCoercer(Default(7000), ParseInt()),
            ),
        ],
        factory=_DispatchDTO,
    )
    flat = ConfigBundle.from_sources(
        [DictSource({ConfigPath.parse("svc.name"): StringValue("api")})]
    )
    got = flat.get(schema, "svc")
    assert got == _DispatchDTO(name="api", port=7000)


def test_get_rejects_non_dataclass_non_schema():
    flat = ConfigBundle.from_sources([DictSource({})])
    with pytest.raises(TypeError, match="dataclass-тип или ObjectSchema"):
        flat.get("not-a-type", "svc")  # type: ignore[arg-type]


def test_get_falls_back_to_classvar_schema_bridge():
    """Bridge: если у dataclass есть SCHEMA-атрибут, он используется поверх автогена."""

    @dataclass(frozen=True)
    class Bridged:
        name: str = "default"

    bridged_schema: ObjectSchema[Bridged] = ObjectSchema(
        fields=[
            FieldSpec(
                name="name",
                coercer=ChainCoercer(Default("from-classvar"), ParseString()),
                description="bridge",
            ),
        ],
        factory=Bridged,
        description="custom bridge schema",
    )
    Bridged.SCHEMA = bridged_schema  # type: ignore[attr-defined]

    flat = ConfigBundle.from_sources([DictSource({})])
    got = flat.get(Bridged, "svc")
    # Если бы был чистый автоген — default был бы "default" (dataclass-поле).
    # Bridge подхватил ClassVar и вернул "from-classvar".
    assert got == Bridged(name="from-classvar")


# Re-export NameSegment чтобы pyright не считал импорт неиспользованным.
_ = NameSegment
