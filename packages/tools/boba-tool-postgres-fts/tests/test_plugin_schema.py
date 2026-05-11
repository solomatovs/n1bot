"""Snapshot-тест: schema_from_dataclass(PostgresFtsPluginConfig) и enable-конвенция."""

from __future__ import annotations

from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigPath
from boba.config.source.dict import DictSource
from boba.plugin import ExtensionContext, install_plugins
from boba.plugin.prompt import PromptOverlay
from boba.schema import schema_from_dataclass
from boba.schema.coercion import MISSING
from boba.schema.declaration import CollectionField, FieldSpec, NestedField
from boba.schema.value import BoolValue, IntValue, StringValue
from boba.tool.postgres_fts import PostgresFtsPlugin, PostgresFtsPluginConfig

_SCHEMA = schema_from_dataclass(PostgresFtsPluginConfig)


def _field(name: str) -> FieldSpec:
    f = next(x for x in _SCHEMA.fields if x.name == name)
    assert isinstance(f, FieldSpec)
    return f


# Структура схемы


def test_factory_is_dto():
    assert _SCHEMA.factory is PostgresFtsPluginConfig


def test_schema_description_from_dto_docstring():
    assert _SCHEMA.description.startswith("PG FTS read-tools")


def test_top_level_field_names():
    expected = {
        "dsn",
        "indexes",
        "min_pool_size",
        "max_pool_size",
        "connect_timeout_sec",
        "snippet_options",
        "max_top_k",
        "fts_search",
        "fts_list_indexes",
    }
    assert {f.name for f in _SCHEMA.fields} == expected


def test_indexes_is_collection_of_index_spec():
    f = next(x for x in _SCHEMA.fields if x.name == "indexes")
    assert isinstance(f, CollectionField)


def test_prompt_overlay_fields_are_nested():
    for name in ("fts_search", "fts_list_indexes"):
        f = next(x for x in _SCHEMA.fields if x.name == name)
        assert isinstance(f, NestedField)
        assert f.schema.factory is PromptOverlay


# Coercer-цепочки скаляров


def test_dsn_required_and_parses_string():
    import pytest

    from boba.patterns import MissingValueError

    f = _field("dsn")
    with pytest.raises(MissingValueError):
        f.coercer.apply(MISSING)
    assert f.coercer.apply(StringValue("postgresql://x/y")) == "postgresql://x/y"


def test_pool_size_defaults():
    assert _field("min_pool_size").coercer.apply(MISSING) == 1
    assert _field("max_pool_size").coercer.apply(MISSING) == 4


def test_connect_timeout_default_and_parses():
    f = _field("connect_timeout_sec")
    assert f.coercer.apply(MISSING) == 10.0
    assert f.coercer.apply(StringValue("3.5")) == 3.5


def test_snippet_options_default():
    f = _field("snippet_options")
    assert f.coercer.apply(MISSING) == "MaxFragments=2,MaxWords=35,MinWords=15"


def test_max_top_k_default_and_min_value():
    import pytest

    from boba.patterns import ConverterInputError

    f = _field("max_top_k")
    assert f.coercer.apply(MISSING) == 20
    with pytest.raises(ConverterInputError):
        f.coercer.apply(IntValue(0))


# Enable-конвенция через install_plugins


def test_disabled_by_default_yields_no_artifacts():
    bundle = ConfigBundle.from_sources([DictSource({})])
    artifacts = list(
        install_plugins(bundle, [PostgresFtsPlugin], ExtensionContext()),
    )
    assert artifacts == []


def test_disabled_explicit_yields_no_artifacts():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {ConfigPath.parse("tool.postgres_fts.enable"): BoolValue(False)},
            ),
        ],
    )
    artifacts = list(
        install_plugins(bundle, [PostgresFtsPlugin], ExtensionContext()),
    )
    assert artifacts == []
