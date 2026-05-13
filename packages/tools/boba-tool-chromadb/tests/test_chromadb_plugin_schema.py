"""Snapshot-тест: schema_from_dataclass(ChromadbPluginConfig) и enable-конвенция."""

from __future__ import annotations

from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigPath
from boba.config.source.dict import DictSource
from boba.plugin import ExtensionContext, install_plugins
from boba.plugin.prompt import PromptOverlay
from boba.schema import schema_from_dataclass
from boba.schema.coercion import MISSING
from boba.schema.declaration import FieldSpec, NestedField
from boba.schema.value import BoolValue, IntValue, StringValue
from boba.tool.chromadb import ChromadbPlugin, ChromadbPluginConfig

import pytest

_SCHEMA = schema_from_dataclass(ChromadbPluginConfig)


def _field(name: str) -> FieldSpec:
    f = next(x for x in _SCHEMA.fields if x.name == name)
    assert isinstance(f, FieldSpec)
    return f


# Структура схемы


def test_factory_is_dto():
    assert _SCHEMA.factory is ChromadbPluginConfig


def test_schema_description_from_dto_docstring():
    assert _SCHEMA.description.startswith("ChromaDB read-tools")


def test_top_level_field_names():
    expected = {
        "persist_path",
        "embedding_model",
        "embedding_base_url",
        "embedding_api_key",
        "snippet_chars",
        "max_top_k",
        "kb_search",
        "kb_list_collections",
    }
    assert {f.name for f in _SCHEMA.fields} == expected


def test_prompt_overlay_fields_are_nested():
    for name in ("kb_search", "kb_list_collections"):
        f = next(x for x in _SCHEMA.fields if x.name == name)
        assert isinstance(f, NestedField)
        assert f.schema.factory is PromptOverlay


# Coercer-цепочки скаляров


def test_persist_path_required_and_parses_string():
    from boba.patterns import MissingValueError

    f = _field("persist_path")
    assert f.description == "Путь к persistent ChromaDB."
    with pytest.raises(MissingValueError):
        f.coercer.apply(MISSING)
    assert f.coercer.apply(StringValue("/tmp/kb")) == "/tmp/kb"


def test_embedding_model_default_is_default_str():
    f = _field("embedding_model")
    assert f.coercer.apply(MISSING) == "default"
    assert f.coercer.apply(StringValue("text-embedding-3-small")) == "text-embedding-3-small"


def test_embedding_base_url_default_empty():
    f = _field("embedding_base_url")
    assert f.coercer.apply(MISSING) == ""


def test_embedding_api_key_default_empty():
    f = _field("embedding_api_key")
    assert f.coercer.apply(MISSING) == ""


def test_snippet_chars_defaults_and_parses_string_int():
    f = _field("snippet_chars")
    assert f.coercer.apply(MISSING) == 300
    # ENV-style строковый int должен парситься.
    assert f.coercer.apply(StringValue("500")) == 500


def test_max_top_k_defaults_and_parses_string_int():
    f = _field("max_top_k")
    assert f.coercer.apply(MISSING) == 20
    assert f.coercer.apply(StringValue("50")) == 50


def test_snippet_chars_min_value_enforced():
    import pytest

    from boba.patterns import ConverterInputError

    f = _field("snippet_chars")
    with pytest.raises(ConverterInputError):
        f.coercer.apply(IntValue(0))


def test_max_top_k_min_value_enforced():
    import pytest

    from boba.patterns import ConverterInputError

    f = _field("max_top_k")
    with pytest.raises(ConverterInputError):
        f.coercer.apply(IntValue(0))


# Enable-конвенция через install_plugins (TConfig резолвится из __orig_bases__)


def test_disabled_by_default_yields_no_artifacts():
    bundle = ConfigBundle.from_sources([DictSource({})])
    artifacts = list(install_plugins(bundle, [ChromadbPlugin], ExtensionContext()))
    assert artifacts == []


def test_disabled_explicit_yields_no_artifacts():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {ConfigPath.parse("tool.chromadb.enable"): BoolValue(False)},
            ),
        ],
    )
    artifacts = list(install_plugins(bundle, [ChromadbPlugin], ExtensionContext()))
    assert artifacts == []
