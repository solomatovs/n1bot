"""CollectionField со ScalarItem: коллекции примитивов из конфига.

- IndexedShape × ScalarItem — `models = ["qwen3", "gemini"]` → tuple[str, ...].
- KeyedShape × ScalarItem    — `[descriptions] foo="..." bar="..."` → dict[str, str].
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from boba.config.bundle import ConfigBundle, FlatConfigMaterializer
from boba.config.path import ConfigPath, ConfigSource
from boba.declaration import (
    CollectionField,
    FieldPathError,
    FieldSpec,
    IndexedShape,
    KeyedShape,
    ObjectSchema,
    ScalarItem,
)
from boba.validators import (
    ChainConverter,
    Default,
    MaxValue,
    MinValue,
    NonEmpty,
    NotNull,
    ParseInt,
    ParseString,
)
from boba.value import ConfigValue, IntValue, StringValue


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
class _ChainlitConfig:
    models: tuple[str, ...]
    ports: tuple[int, ...] = ()


_CHAINLIT_SCHEMA: ObjectSchema[_ChainlitConfig] = ObjectSchema(
    fields=[
        CollectionField(
            name="models",
            reader=ScalarItem(ChainConverter(NotNull(), ParseString(), NonEmpty())),
            shape=IndexedShape(),
        ),
        CollectionField(
            name="ports",
            reader=ScalarItem(
                ChainConverter(NotNull(), ParseInt(), MinValue(1), MaxValue(65535))
            ),
            shape=IndexedShape(),
        ),
    ],
    factory=_ChainlitConfig,
)


def test_scalar_list_from_dict_source():
    flat = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$chainlit.models[0]"): StringValue("qwen3"),
                    ConfigPath.parse("$chainlit.models[1]"): StringValue("gemini"),
                    ConfigPath.parse("$chainlit.models[2]"): StringValue("deepseek"),
                    ConfigPath.parse("$chainlit.ports[0]"): IntValue(8501),
                    ConfigPath.parse("$chainlit.ports[1]"): IntValue(8502),
                }
            )
        ]
    ).flat
    cfg = FlatConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
        flat, ConfigPath.parse("$chainlit")
    )
    assert cfg.models == ("qwen3", "gemini", "deepseek")
    assert cfg.ports == (8501, 8502)


def test_scalar_list_empty_when_absent():
    flat = ConfigBundle.from_sources([_DictSource({})]).flat
    cfg = FlatConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
        flat, ConfigPath.parse("$chainlit")
    )
    assert cfg.models == ()
    assert cfg.ports == ()


def test_scalar_list_sorted_by_index_regardless_of_order():
    flat = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$chainlit.models[2]"): StringValue("c"),
                    ConfigPath.parse("$chainlit.models[0]"): StringValue("a"),
                    ConfigPath.parse("$chainlit.models[1]"): StringValue("b"),
                }
            )
        ]
    ).flat
    cfg = FlatConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
        flat, ConfigPath.parse("$chainlit")
    )
    assert cfg.models == ("a", "b", "c")


def test_scalar_list_item_validation_error_carries_index():
    flat = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$chainlit.models[0]"): StringValue(
                        ""
                    ),  # NonEmpty fails
                }
            )
        ]
    ).flat
    with pytest.raises(FieldPathError) as info:
        FlatConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
            flat, ConfigPath.parse("$chainlit")
        )
    assert info.value.field_name == "models"
    assert "[0]" in info.value.location


# ──────────────────── KeyedShape × ScalarItem — словарь скаляров ────────────────────


@dataclass(frozen=True)
class _ToolDescriptions:
    descriptions: Mapping[str, str]
    limits: Mapping[str, int]


_TOOLS_SCHEMA: ObjectSchema[_ToolDescriptions] = ObjectSchema(
    fields=[
        CollectionField(
            name="descriptions",
            reader=ScalarItem(ChainConverter(Default(""), ParseString())),
            shape=KeyedShape(),
        ),
        CollectionField(
            name="limits",
            reader=ScalarItem(ChainConverter(Default(0), ParseInt(), MinValue(0))),
            shape=KeyedShape(),
        ),
    ],
    factory=_ToolDescriptions,
)


def test_mapping_scalar_from_dict_source():
    flat = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$tools.descriptions.kb_search"): StringValue(
                        "Поиск"
                    ),
                    ConfigPath.parse("$tools.descriptions.html_outline"): StringValue(
                        "Оглавление"
                    ),
                    ConfigPath.parse("$tools.limits.kb_search"): IntValue(20),
                }
            )
        ]
    ).flat
    cfg = FlatConfigMaterializer(_TOOLS_SCHEMA).materialize(
        flat, ConfigPath.parse("$tools")
    )
    assert cfg.descriptions == {"kb_search": "Поиск", "html_outline": "Оглавление"}
    assert cfg.limits == {"kb_search": 20}


def test_mapping_scalar_empty_when_absent():
    flat = ConfigBundle.from_sources([_DictSource({})]).flat
    cfg = FlatConfigMaterializer(_TOOLS_SCHEMA).materialize(
        flat, ConfigPath.parse("$tools")
    )
    assert cfg.descriptions == {}
    assert cfg.limits == {}


def test_mapping_scalar_item_validation_error_carries_key():
    flat = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$tools.limits.kb_search"): IntValue(
                        -1
                    ),  # MinValue(0) fails
                }
            )
        ]
    ).flat
    with pytest.raises(FieldPathError) as info:
        FlatConfigMaterializer(_TOOLS_SCHEMA).materialize(
            flat, ConfigPath.parse("$tools")
        )
    assert info.value.field_name == "limits"
    assert "kb_search" in info.value.location


# Re-export FieldSpec — чтобы pyright не считал импорт неиспользованным.
_ = FieldSpec
