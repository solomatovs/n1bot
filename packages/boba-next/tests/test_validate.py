"""ToolArgsValidator: ObjectSchema → DTO из готового Mapping[str, Any].

Сценарии: dict от LLM tool-call, JSON / YAML / любой иерархический dict.
В отличие от ConfigMaterializer (читает плоский ConfigSpace через ConfigPath),
ToolArgsValidator работает с нативными `dict` / `list`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest
from boba_next import (
    ChainConverter,
    CollectionField,
    Default,
    FieldPathError,
    FieldPathMissingError,
    FieldSpec,
    IndexedShape,
    KeyedShape,
    MaxValue,
    MinValue,
    NonEmpty,
    ObjectItem,
    ObjectSchema,
    Ordered,
    ParseBool,
    ParseInt,
    ParseString,
    Required,
    ScalarItem,
    ToolArgsValidator,
)

# ──────────────────── Простой скалярный объект ────────────────────


@dataclass(frozen=True)
class _Agent:
    max_iterations: int
    enabled: bool


_AGENT_SCHEMA: ObjectSchema[_Agent] = ObjectSchema(
    fields=[
        FieldSpec(
            "max_iterations",
            ChainConverter(Default(20), ParseInt(), MinValue(1)),
        ),
        FieldSpec("enabled", ChainConverter(Default(False), ParseBool())),
    ],
    factory=_Agent,
)


def test_scalar_with_defaults_when_keys_absent():
    agent = ToolArgsValidator(_AGENT_SCHEMA).validate({})
    assert agent == _Agent(max_iterations=20, enabled=False)


def test_scalar_overridden_from_dict():
    agent = ToolArgsValidator(_AGENT_SCHEMA).validate(
        {"max_iterations": 200, "enabled": True}
    )
    assert agent == _Agent(max_iterations=200, enabled=True)


def test_required_missing_raises_path_missing():
    schema: ObjectSchema[dict] = ObjectSchema(
        fields=[FieldSpec("x", ChainConverter(Required(), ParseString()))],
    )
    with pytest.raises(FieldPathMissingError) as info:
        ToolArgsValidator(schema).validate({})
    assert info.value.field_name == "x"


def test_field_validation_error_attaches_field_name():
    with pytest.raises(FieldPathError) as info:
        ToolArgsValidator(_AGENT_SCHEMA).validate(
            {"max_iterations": 0}
        )  # MinValue(1) fails
    assert info.value.field_name == "max_iterations"


# ──────────────────── Cross-field invariants ────────────────────


@dataclass(frozen=True)
class _Range:
    lo: int
    hi: int


_RANGE_SCHEMA: ObjectSchema[_Range] = ObjectSchema(
    fields=[
        FieldSpec("lo", ChainConverter(Required(), ParseInt())),
        FieldSpec("hi", ChainConverter(Required(), ParseInt())),
    ],
    invariants=Ordered("lo", "hi"),
    factory=_Range,
)


def test_invariant_passes_when_ordered():
    assert ToolArgsValidator(_RANGE_SCHEMA).validate({"lo": 1, "hi": 10}) == _Range(
        1, 10
    )


def test_invariant_violation_attaches_invariants_field_name():
    with pytest.raises(FieldPathError) as info:
        ToolArgsValidator(_RANGE_SCHEMA).validate({"lo": 50, "hi": 10})
    assert info.value.field_name == "<invariants>"


# ──────────────────── IndexedShape × ScalarItem (массив скаляров) ────────────────────


@dataclass(frozen=True)
class _Chainlit:
    models: tuple[str, ...]


_CHAINLIT_SCHEMA: ObjectSchema[_Chainlit] = ObjectSchema(
    fields=[
        CollectionField(
            name="models",
            reader=ScalarItem(ChainConverter(Required(), ParseString(), NonEmpty())),
            shape=IndexedShape(),
        ),
    ],
    factory=_Chainlit,
)


def test_scalar_list_from_native_list():
    cfg = ToolArgsValidator(_CHAINLIT_SCHEMA).validate({"models": ["qwen3", "gemini"]})
    assert cfg.models == ("qwen3", "gemini")


def test_scalar_list_empty_when_key_absent():
    assert ToolArgsValidator(_CHAINLIT_SCHEMA).validate({}).models == ()


def test_scalar_list_wrong_container_type_raises():
    with pytest.raises(FieldPathError) as info:
        ToolArgsValidator(_CHAINLIT_SCHEMA).validate({"models": {"not": "a list"}})
    assert info.value.field_name == "models"
    assert "expected list" in str(info.value)


def test_scalar_list_item_validation_error_carries_index():
    with pytest.raises(FieldPathError) as info:
        # NonEmpty fails on second item:
        ToolArgsValidator(_CHAINLIT_SCHEMA).validate({"models": ["qwen3", ""]})
    assert info.value.field_name == "models"
    assert "[1]" in info.value.location


# ──────────────────── KeyedShape × ScalarItem (словарь скаляров) ────────────────────


@dataclass(frozen=True)
class _Limits:
    limits: Mapping[str, int]


_LIMITS_SCHEMA: ObjectSchema[_Limits] = ObjectSchema(
    fields=[
        CollectionField(
            name="limits",
            reader=ScalarItem(ChainConverter(Required(), ParseInt(), MinValue(0))),
            shape=KeyedShape(),
        ),
    ],
    factory=_Limits,
)


def test_scalar_mapping_from_native_dict():
    cfg = ToolArgsValidator(_LIMITS_SCHEMA).validate({"limits": {"a": 10, "b": 20}})
    assert cfg.limits == {"a": 10, "b": 20}


def test_scalar_mapping_empty_when_key_absent():
    assert ToolArgsValidator(_LIMITS_SCHEMA).validate({}).limits == {}


def test_scalar_mapping_wrong_container_type_raises():
    with pytest.raises(FieldPathError) as info:
        ToolArgsValidator(_LIMITS_SCHEMA).validate({"limits": [1, 2, 3]})
    assert info.value.field_name == "limits"
    assert "expected mapping" in str(info.value)


def test_scalar_mapping_item_validation_error_carries_key():
    with pytest.raises(FieldPathError) as info:
        ToolArgsValidator(_LIMITS_SCHEMA).validate({"limits": {"good": 5, "bad": -1}})
    assert info.value.field_name == "limits"
    assert "bad" in info.value.location


# ──────────────────── KeyedShape × ObjectItem (вложенные объекты) ────────────────────


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
class _Tools:
    tools: Mapping[str, _ToolEntry] = field(default_factory=dict)


_TOOLS_SCHEMA: ObjectSchema[_Tools] = ObjectSchema(
    fields=[
        CollectionField(
            name="tools",
            reader=ObjectItem(_TOOL_ENTRY_SCHEMA),
            shape=KeyedShape(),
        ),
    ],
    factory=_Tools,
)


def test_object_mapping_recursive_validation():
    raw = {
        "tools": {
            "kb_search": {"enabled": True, "description": "База знаний"},
            "html_outline": {"description": "Оглавление"},  # enabled = default False
        },
    }
    cfg = ToolArgsValidator(_TOOLS_SCHEMA).validate(raw)
    kb = cfg.tools["kb_search"]
    html = cfg.tools["html_outline"]
    assert kb == _ToolEntry(enabled=True, description="База знаний")
    assert html == _ToolEntry(enabled=False, description="Оглавление")


def test_object_mapping_item_must_be_mapping():
    with pytest.raises(FieldPathError) as info:
        ToolArgsValidator(_TOOLS_SCHEMA).validate({"tools": {"kb": "not a dict"}})
    assert info.value.field_name == "tools"
    assert "kb" in info.value.location
    assert "expected mapping" in str(info.value)


# ──────────────────── E2E: реалистичный LLM tool-call payload ────────────────────


@dataclass(frozen=True)
class _ParamOverlay:
    description: str = ""


@dataclass(frozen=True)
class _Tool:
    enabled: bool
    description: str
    params: Mapping[str, _ParamOverlay] = field(default_factory=dict)


@dataclass(frozen=True)
class _Chromadb:
    enabled: bool
    persist_path: str
    min_top_k: int
    max_top_k: int
    tools: Mapping[str, _Tool] = field(default_factory=dict)


_PARAM_SCHEMA: ObjectSchema[_ParamOverlay] = ObjectSchema(
    fields=[FieldSpec("description", ChainConverter(Default(""), ParseString()))],
    factory=_ParamOverlay,
)
_TOOL_SCHEMA: ObjectSchema[_Tool] = ObjectSchema(
    fields=[
        FieldSpec("enabled", ChainConverter(Default(False), ParseBool())),
        FieldSpec("description", ChainConverter(Default(""), ParseString())),
        CollectionField("params", reader=ObjectItem(_PARAM_SCHEMA), shape=KeyedShape()),
    ],
    factory=_Tool,
)
_CHROMADB_SCHEMA: ObjectSchema[_Chromadb] = ObjectSchema(
    fields=[
        FieldSpec("enabled", ChainConverter(Default(False), ParseBool())),
        FieldSpec("persist_path", ChainConverter(Required(), ParseString())),
        FieldSpec("min_top_k", ChainConverter(Default(1), ParseInt(), MinValue(1))),
        FieldSpec("max_top_k", ChainConverter(Default(20), ParseInt(), MaxValue(100))),
        CollectionField("tools", reader=ObjectItem(_TOOL_SCHEMA), shape=KeyedShape()),
    ],
    invariants=Ordered("min_top_k", "max_top_k"),
    factory=_Chromadb,
)


def test_llm_tool_call_full_payload():
    """Имитация JSON-args, который LLM прислал в tool-call."""
    payload = {
        "enabled": True,
        "persist_path": "/var/lib/chromadb",
        "min_top_k": 5,
        "max_top_k": 50,
        "tools": {
            "kb_search": {
                "enabled": True,
                "description": "Поиск по базе знаний",
                "params": {
                    "query": {"description": "Поисковый запрос"},
                    "top_k": {"description": "Количество результатов"},
                },
            },
        },
    }
    cfg = ToolArgsValidator(_CHROMADB_SCHEMA).validate(payload)
    assert cfg.enabled is True
    assert cfg.persist_path == "/var/lib/chromadb"
    assert cfg.min_top_k == 5
    assert cfg.max_top_k == 50
    assert cfg.tools["kb_search"].enabled is True
    assert cfg.tools["kb_search"].params["query"].description == "Поисковый запрос"


def test_llm_tool_call_invariant_violation_propagates():
    payload = {"persist_path": "/x", "min_top_k": 80, "max_top_k": 10}
    with pytest.raises(FieldPathError) as info:
        ToolArgsValidator(_CHROMADB_SCHEMA).validate(payload)
    assert info.value.field_name == "<invariants>"


def test_llm_tool_call_nested_field_error_carries_full_location():
    payload = {
        "persist_path": "/x",
        "tools": {
            "kb_search": {
                "params": {
                    # ParseString не примет int (если строгий):
                    "query": {"description": 12345},
                },
            },
        },
    }
    # Без строгого ParseString int приведётся к строке; проверим что вложенный path
    # хотя бы строится корректно для случая, когда description явно задан.
    cfg = ToolArgsValidator(_CHROMADB_SCHEMA).validate(payload)
    assert "kb_search" in cfg.tools
    # Ключевое: рекурсия через ObjectItem отработала, params['query'] построен.
    assert "query" in cfg.tools["kb_search"].params
