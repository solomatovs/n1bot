"""Snapshot-тест: schema_from_dataclass(ConfluencePluginConfig).

Покрывает:
  * factory, description, имена полей;
  * Required/Default/ParseString/ParseFloat скаляры;
  * Literal[...] → OneOf (auth_method, body_format);
  * object-level invariant из @schema(invariants=...) — RequiredWhen;
  * NestedField для PromptOverlay-полей.
"""

from __future__ import annotations

import pytest

from boba.config.bundle import ConfigBundle, FlatConfigMaterializer
from boba.config.path import ConfigPath
from boba.config.source.dict import DictSource
from boba.patterns import ConverterInputError, MissingValueError
from boba.plugin.prompt import PromptOverlay
from boba.schema import schema_from_dataclass
from boba.schema.coercion import MISSING
from boba.schema.coercion.invariants import RequiredWhen
from boba.schema.declaration import FieldSpec, NestedField
from boba.schema.value import FloatValue, StringValue
from boba.tool.confluence import ConfluencePluginConfig

_SCHEMA = schema_from_dataclass(ConfluencePluginConfig)


def _field(name: str) -> FieldSpec:
    f = next(x for x in _SCHEMA.fields if x.name == name)
    assert isinstance(f, FieldSpec)
    return f


# Структура схемы


def test_factory_is_dto():
    assert _SCHEMA.factory is ConfluencePluginConfig


def test_schema_description_from_dto_docstring():
    assert _SCHEMA.description.startswith("Confluence multi-tool plugin")


def test_top_level_field_names():
    expected = {
        "base_url",
        "auth_method",
        "auth_user",
        "auth_token",
        "timeout_sec",
        "body_format",
        "confluence_search",
        "confluence_page_outline",
        "confluence_page_section",
    }
    assert {f.name for f in _SCHEMA.fields} == expected


def test_prompt_overlay_fields_are_nested():
    for name in (
        "confluence_search",
        "confluence_page_outline",
        "confluence_page_section",
    ):
        f = next(x for x in _SCHEMA.fields if x.name == name)
        assert isinstance(f, NestedField)
        assert f.schema.factory is PromptOverlay


# Coercer-цепочки скаляров


def test_base_url_required():
    f = _field("base_url")
    with pytest.raises(MissingValueError):
        f.coercer.apply(MISSING)
    assert f.coercer.apply(StringValue("https://confluence.example")) == (
        "https://confluence.example"
    )


def test_auth_token_required():
    f = _field("auth_token")
    with pytest.raises(MissingValueError):
        f.coercer.apply(MISSING)


def test_auth_method_default_pat_and_oneof_enforced():
    f = _field("auth_method")
    assert f.coercer.apply(MISSING) == "pat"
    assert f.coercer.apply(StringValue("basic")) == "basic"
    with pytest.raises(ConverterInputError):
        f.coercer.apply(StringValue("oauth"))


def test_auth_user_default_empty():
    f = _field("auth_user")
    assert f.coercer.apply(MISSING) == ""


def test_timeout_sec_default_and_parses_string_float():
    f = _field("timeout_sec")
    assert f.coercer.apply(MISSING) == 30.0
    assert f.coercer.apply(StringValue("12.5")) == 12.5
    assert f.coercer.apply(FloatValue(7.0)) == 7.0


def test_body_format_default_view_and_oneof_enforced():
    f = _field("body_format")
    assert f.coercer.apply(MISSING) == "view"
    assert f.coercer.apply(StringValue("export_view")) == "export_view"
    assert f.coercer.apply(StringValue("storage")) == "storage"
    with pytest.raises(ConverterInputError):
        f.coercer.apply(StringValue("wiki"))


# Invariant из @schema


def test_schema_has_invariant_from_decorator():
    assert isinstance(_SCHEMA.invariants, RequiredWhen)


def test_invariant_runs_when_auth_method_basic_without_user():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("tool.confluence.base_url"): StringValue(
                        "https://x",
                    ),
                    ConfigPath.parse("tool.confluence.auth_token"): StringValue(
                        "tok",
                    ),
                    ConfigPath.parse("tool.confluence.auth_method"): StringValue(
                        "basic",
                    ),
                },
            ),
        ],
    ).flat
    with pytest.raises(ConverterInputError, match="auth_method='basic'"):
        FlatConfigMaterializer(_SCHEMA).materialize(
            flat, ConfigPath.parse("tool.confluence"),
        )


def test_invariant_passes_when_auth_method_basic_with_user():
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("tool.confluence.base_url"): StringValue(
                        "https://x",
                    ),
                    ConfigPath.parse("tool.confluence.auth_token"): StringValue(
                        "tok",
                    ),
                    ConfigPath.parse("tool.confluence.auth_method"): StringValue(
                        "basic",
                    ),
                    ConfigPath.parse("tool.confluence.auth_user"): StringValue(
                        "alice",
                    ),
                },
            ),
        ],
    ).flat
    cfg = FlatConfigMaterializer(_SCHEMA).materialize(
        flat, ConfigPath.parse("tool.confluence"),
    )
    assert cfg.auth_method == "basic"
    assert cfg.auth_user == "alice"


def test_invariant_silent_when_auth_method_pat():
    """При auth_method=pat (default) auth_user может быть пустым."""
    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("tool.confluence.base_url"): StringValue(
                        "https://x",
                    ),
                    ConfigPath.parse("tool.confluence.auth_token"): StringValue(
                        "tok",
                    ),
                },
            ),
        ],
    ).flat
    cfg = FlatConfigMaterializer(_SCHEMA).materialize(
        flat, ConfigPath.parse("tool.confluence"),
    )
    assert cfg.auth_method == "pat"
    assert cfg.auth_user == ""
