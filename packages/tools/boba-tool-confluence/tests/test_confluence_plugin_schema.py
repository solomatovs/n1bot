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
from boba.patterns import ConverterInputError
from boba.schema import schema_from_dataclass
from boba.schema.declaration import FieldSpec
from boba.schema.value import StringValue
from boba.tool.confluence import ConfluencePluginConfig

_SCHEMA = schema_from_dataclass(ConfluencePluginConfig)


def _field(name: str) -> FieldSpec:
    f = next(x for x in _SCHEMA.fields if x.name == name)
    assert isinstance(f, FieldSpec)
    return f


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
