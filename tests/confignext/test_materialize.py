"""materialize: ObjectSchema + FieldSpec/MappingField/ListField → DTO."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from boba.domain.core.confignext import (
    BoolValue,
    ChainConverter,
    ConfigPath,
    ConfigSource,
    ConfigValue,
    Default,
    FieldPathError,
    FieldPathMissingError,
    FieldSpec,
    # FlatConfig,
    FlatConfigBuilder,
    IntValue,
    ListField,
    MappingField,
    MinValue,
    NameSegment,
    NonEmpty,
    ObjectSchema,
    ParseBool,
    ParseInt,
    ParseString,
    Required,
    StringValue,
)


class _DictSource(ConfigSource):
    def __init__(self, values: Mapping[ConfigPath, ConfigValue]) -> None:
        self._v = values

    def name(self) -> str:
        return "dict"

    def priority(self) -> int:
        return 100

    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        return dict(self._v)


@dataclass(frozen=True)
class _Agent:
    max_iterations: int
    enabled: bool


_AGENT_SCHEMA: ObjectSchema[_Agent] = ObjectSchema(
    fields=[
        FieldSpec(
            name="max_iterations",
            converter=ChainConverter(Default(20), ParseInt(), MinValue(1)),
        ),
        FieldSpec(
            name="enabled",
            converter=ChainConverter(Default(False), ParseBool()),
        ),
    ],
    factory=_Agent,
)


def test_scalar_with_defaults():
    flat = FlatConfigBuilder.from_sources([_DictSource({})])
    agent = _AGENT_SCHEMA.materialize(flat, ConfigPath.parse("$agent"))
    assert agent == _Agent(max_iterations=20, enabled=False)


def test_scalar_overridden():
    flat = FlatConfigBuilder.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$agent.max_iterations"): IntValue(200),
                    ConfigPath.parse("$agent.enabled"): BoolValue(True),
                }
            )
        ]
    )
    agent = _AGENT_SCHEMA.materialize(flat, ConfigPath.parse("$agent"))
    assert agent == _Agent(max_iterations=200, enabled=True)


def test_required_missing_raises():
    schema: ObjectSchema[dict] = ObjectSchema(
        fields=[FieldSpec("x", ChainConverter(Required(), ParseString()))],
    )
    flat = FlatConfigBuilder.from_sources([_DictSource({})])
    with pytest.raises(FieldPathMissingError):
        schema.materialize(flat, ConfigPath.parse("$root"))


def test_validation_error_attaches_field():
    schema: ObjectSchema[dict] = ObjectSchema(
        fields=[
            FieldSpec(
                "max_iterations",
                ChainConverter(Required(), ParseInt(), MinValue(1)),
            )
        ],
    )
    flat = FlatConfigBuilder.from_sources(
        [_DictSource({ConfigPath.parse("$root.max_iterations"): IntValue(0)})]
    )
    with pytest.raises(FieldPathError) as info:
        schema.materialize(flat, ConfigPath.parse("$root"))
    assert info.value.field_name == "max_iterations"


# ──────── MappingField (динамические подсекции, как tools.<id>) ────────


@dataclass(frozen=True)
class _ToolEntry:
    enabled: bool
    description: str


_TOOL_ENTRY_SCHEMA: ObjectSchema[_ToolEntry] = ObjectSchema(
    fields=[
        FieldSpec("enabled", ChainConverter(Default(False), ParseBool())),
        FieldSpec("description", ChainConverter(Default(""), ParseString())),
    ],
    factory=_ToolEntry,
)


@dataclass(frozen=True)
class _ExtBlock:
    enabled: bool
    tools: Mapping[str, _ToolEntry]


_EXT_SCHEMA: ObjectSchema[_ExtBlock] = ObjectSchema(
    fields=[
        FieldSpec("enabled", ChainConverter(Default(False), ParseBool())),
        MappingField(name="tools", value_schema=_TOOL_ENTRY_SCHEMA),
    ],
    factory=_ExtBlock,
)


def test_mapping_field_collects_dynamic_subsections():
    flat = FlatConfigBuilder.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$ext.html.enabled"): BoolValue(True),
                    ConfigPath.parse(
                        "$ext.html.tools.html_outline.enabled"
                    ): BoolValue(True),
                    ConfigPath.parse(
                        "$ext.html.tools.html_outline.description"
                    ): StringValue("custom outline"),
                    ConfigPath.parse(
                        "$ext.html.tools.html_section.description"
                    ): StringValue("custom section"),
                }
            )
        ]
    )
    block = _EXT_SCHEMA.materialize(flat, ConfigPath.parse("$ext.html"))
    assert block.enabled is True
    assert set(block.tools) == {"html_outline", "html_section"}
    assert block.tools["html_outline"].enabled is True
    assert block.tools["html_outline"].description == "custom outline"
    assert block.tools["html_section"].enabled is False  # default
    assert block.tools["html_section"].description == "custom section"


def test_mapping_field_empty_when_no_subsections():
    flat = FlatConfigBuilder.from_sources(
        [_DictSource({ConfigPath.parse("$ext.html.enabled"): BoolValue(True)})]
    )
    block = _EXT_SCHEMA.materialize(flat, ConfigPath.parse("$ext.html"))
    assert block.tools == {}


# ──────── ListField (индексированные подсекции) ────────


@dataclass(frozen=True)
class _Model:
    name: str


_MODEL_SCHEMA: ObjectSchema[_Model] = ObjectSchema(
    fields=[FieldSpec("name", ChainConverter(Required(), ParseString(), NonEmpty()))],
    factory=_Model,
)


@dataclass(frozen=True)
class _ChainlitBlock:
    models: tuple[_Model, ...]


_CHAINLIT_SCHEMA: ObjectSchema[_ChainlitBlock] = ObjectSchema(
    fields=[
        ListField(name="models", item_schema=_MODEL_SCHEMA),
    ],
    factory=_ChainlitBlock,
)


def test_list_field_collects_indexed_subsections():
    flat = FlatConfigBuilder.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$chainlit.models[0].name"): StringValue("qwen3"),
                    ConfigPath.parse("$chainlit.models[1].name"): StringValue("gemini"),
                    ConfigPath.parse("$chainlit.models[2].name"): StringValue(
                        "deepseek"
                    ),
                }
            )
        ]
    )
    block = _CHAINLIT_SCHEMA.materialize(flat, ConfigPath.parse("$chainlit"))
    assert tuple(m.name for m in block.models) == ("qwen3", "gemini", "deepseek")


def test_list_field_uses_segment_indices_in_sorted_order():
    flat = FlatConfigBuilder.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$chainlit.models[2].name"): StringValue("c"),
                    ConfigPath.parse("$chainlit.models[0].name"): StringValue("a"),
                    ConfigPath.parse("$chainlit.models[1].name"): StringValue("b"),
                }
            )
        ]
    )
    block = _CHAINLIT_SCHEMA.materialize(flat, ConfigPath.parse("$chainlit"))
    assert tuple(m.name for m in block.models) == ("a", "b", "c")


def test_nested_field_error_propagates_with_location():
    flat = FlatConfigBuilder.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$ext.html.tools.html_outline.enabled"): IntValue(
                        42
                    )  # bool ожидался
                }
            )
        ]
    )
    with pytest.raises(FieldPathError) as info:
        _EXT_SCHEMA.materialize(flat, ConfigPath.parse("$ext.html"))
    assert "tools" in info.value.field_name or "tools" in str(info.value)
    assert "html_outline" in str(info.value)


# Re-export NameSegment чтобы pyright не считал импорт неиспользованным.
_ = NameSegment
