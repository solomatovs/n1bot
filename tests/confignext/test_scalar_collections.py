"""CollectionField со ScalarItem: коллекции примитивов из конфига.

- IndexedShape × ScalarItem — `models = ["qwen3", "gemini"]` → tuple[str, ...].
- KeyedShape × ScalarItem    — `[descriptions] foo="..." bar="..."` → dict[str, str].
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from boba.config.toml.next import TomlSource
from boba.domain.core.confignext import (
    ChainConverter,
    CollectionField,
    ConfigMaterializer,
    ConfigPath,
    ConfigSource,
    ConfigValue,
    Default,
    FieldPathError,
    FieldSpec,
    FlatConfigBuilder,
    IndexedShape,
    IntValue,
    KeyedShape,
    MaxValue,
    MinValue,
    NonEmpty,
    ObjectSchema,
    ParseInt,
    ParseString,
    Required,
    ScalarItem,
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


def _toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(content, encoding="utf-8")
    return p


@dataclass(frozen=True)
class _ChainlitConfig:
    models: tuple[str, ...]
    ports: tuple[int, ...] = ()


_CHAINLIT_SCHEMA: ObjectSchema[_ChainlitConfig] = ObjectSchema(
    fields=[
        CollectionField(
            name="models",
            reader=ScalarItem(ChainConverter(Required(), ParseString(), NonEmpty())),
            shape=IndexedShape(),
        ),
        CollectionField(
            name="ports",
            reader=ScalarItem(
                ChainConverter(Required(), ParseInt(), MinValue(1), MaxValue(65535))
            ),
            shape=IndexedShape(),
        ),
    ],
    factory=_ChainlitConfig,
)


def test_scalar_list_from_dict_source():
    flat = FlatConfigBuilder.from_sources(
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
    )
    cfg = ConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
        flat, ConfigPath.parse("$chainlit")
    )
    assert cfg.models == ("qwen3", "gemini", "deepseek")
    assert cfg.ports == (8501, 8502)


def test_scalar_list_empty_when_absent():
    flat = FlatConfigBuilder.from_sources([_DictSource({})])
    cfg = ConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
        flat, ConfigPath.parse("$chainlit")
    )
    assert cfg.models == ()
    assert cfg.ports == ()


def test_scalar_list_sorted_by_index_regardless_of_order():
    flat = FlatConfigBuilder.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$chainlit.models[2]"): StringValue("c"),
                    ConfigPath.parse("$chainlit.models[0]"): StringValue("a"),
                    ConfigPath.parse("$chainlit.models[1]"): StringValue("b"),
                }
            )
        ]
    )
    cfg = ConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
        flat, ConfigPath.parse("$chainlit")
    )
    assert cfg.models == ("a", "b", "c")


def test_scalar_list_from_toml(tmp_path: Path):
    cfg_file = _toml(
        tmp_path,
        """
        [chainlit]
        models = ["qwen3", "gemini", "deepseek"]
        ports = [8501, 8502]
        """,
    )
    flat = FlatConfigBuilder.from_sources([TomlSource(cfg_file)])
    cfg = ConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
        flat, ConfigPath.parse("$chainlit")
    )
    assert cfg.models == ("qwen3", "gemini", "deepseek")
    assert cfg.ports == (8501, 8502)


def test_scalar_list_item_validation_error_carries_index():
    flat = FlatConfigBuilder.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$chainlit.models[0]"): StringValue(
                        ""
                    ),  # NonEmpty fails
                }
            )
        ]
    )
    with pytest.raises(FieldPathError) as info:
        ConfigMaterializer(_CHAINLIT_SCHEMA).materialize(
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
    flat = FlatConfigBuilder.from_sources(
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
    )
    cfg = ConfigMaterializer(_TOOLS_SCHEMA).materialize(
        flat, ConfigPath.parse("$tools")
    )
    assert cfg.descriptions == {"kb_search": "Поиск", "html_outline": "Оглавление"}
    assert cfg.limits == {"kb_search": 20}


def test_mapping_scalar_empty_when_absent():
    flat = FlatConfigBuilder.from_sources([_DictSource({})])
    cfg = ConfigMaterializer(_TOOLS_SCHEMA).materialize(
        flat, ConfigPath.parse("$tools")
    )
    assert cfg.descriptions == {}
    assert cfg.limits == {}


def test_mapping_scalar_from_toml(tmp_path: Path):
    cfg_file = _toml(
        tmp_path,
        """
        [tools.descriptions]
        kb_search = "Поиск по базе"
        html_outline = "Оглавление HTML"

        [tools.limits]
        kb_search = 50
        html_outline = 200
        """,
    )
    flat = FlatConfigBuilder.from_sources([TomlSource(cfg_file)])
    cfg = ConfigMaterializer(_TOOLS_SCHEMA).materialize(
        flat, ConfigPath.parse("$tools")
    )
    assert cfg.descriptions["kb_search"] == "Поиск по базе"
    assert cfg.descriptions["html_outline"] == "Оглавление HTML"
    assert cfg.limits == {"kb_search": 50, "html_outline": 200}


def test_mapping_scalar_inline_table_from_toml(tmp_path: Path):
    # TOML inline-table эквивалентен подсекциям.
    cfg_file = _toml(
        tmp_path,
        """
        [tools]
        descriptions = { kb_search = "x", html_outline = "y" }
        limits = { kb_search = 5 }
        """,
    )
    flat = FlatConfigBuilder.from_sources([TomlSource(cfg_file)])
    cfg = ConfigMaterializer(_TOOLS_SCHEMA).materialize(
        flat, ConfigPath.parse("$tools")
    )
    assert cfg.descriptions == {"kb_search": "x", "html_outline": "y"}
    assert cfg.limits == {"kb_search": 5}


def test_mapping_scalar_item_validation_error_carries_key():
    flat = FlatConfigBuilder.from_sources(
        [
            _DictSource(
                {
                    ConfigPath.parse("$tools.limits.kb_search"): IntValue(
                        -1
                    ),  # MinValue(0) fails
                }
            )
        ]
    )
    with pytest.raises(FieldPathError) as info:
        ConfigMaterializer(_TOOLS_SCHEMA).materialize(flat, ConfigPath.parse("$tools"))
    assert info.value.field_name == "limits"
    assert "kb_search" in info.value.location


# Re-export FieldSpec — чтобы pyright не считал импорт неиспользованным.
_ = FieldSpec
