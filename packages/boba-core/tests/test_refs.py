"""ReferenceResolver: разрешение `@{path}`-ссылок в FlatConfig."""

from __future__ import annotations

import pytest

from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigPath
from boba.config.refs import (
    CircularRefError,
    OriginStep,
    RefDepthExceededError,
    ReferenceResolver,
    UnresolvedRefError,
)
from boba.config.source.dict import DictSource
from boba.value import (
    BoolValue,
    ConfigValue,
    IntValue,
    StringValue,
)

# --- Полная замена ---


def test_full_replacement_string_value():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("base.url"): StringValue("https://example.com"),
                    ConfigPath.parse("tool.search.url"): StringValue("@{base.url}"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(ConfigPath.parse("tool.search.url")).value() == StringValue(
        "https://example.com"
    )


def test_reference_preserves_non_string_value_type():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("base.timeout"): IntValue(30),
                    ConfigPath.parse("tool.search.timeout"): StringValue("@{base.timeout}"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(ConfigPath.parse("tool.search.timeout")).value() == IntValue(30)


def test_reference_preserves_bool_value_type():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("base.flag"): BoolValue(True),
                    ConfigPath.parse("tool.flag"): StringValue("@{base.flag}"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(ConfigPath.parse("tool.flag")).value() == BoolValue(True)


def test_partial_template_is_not_a_reference():
    """`prefix-@{X}-suffix` — обычная строка, не интерполируется."""
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("base.host"): StringValue("example.com"),
                    ConfigPath.parse("tool.url"): StringValue("https://@{base.host}/api"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(ConfigPath.parse("tool.url")).value() == StringValue(
        "https://@{base.host}/api"
    )


def test_non_reference_value_unchanged():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("tool.name"): StringValue("plain string"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(ConfigPath.parse("tool.name")).value() == StringValue(
        "plain string"
    )


# --- Цепочки ---


def test_chain_of_references_resolves_to_final():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("root.value"): IntValue(42),
                    ConfigPath.parse("mid.value"): StringValue("@{root.value}"),
                    ConfigPath.parse("leaf.value"): StringValue("@{mid.value}"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(ConfigPath.parse("leaf.value")).value() == IntValue(42)


# --- Ошибки ---


def test_unresolved_reference_raises():
    src = DictSource(
        {ConfigPath.parse("tool.url"): StringValue("@{base.does_not_exist}")},
    )
    with pytest.raises(UnresolvedRefError) as info:
        ConfigBundle.from_sources([src])
    assert info.value.target == ConfigPath.parse("base.does_not_exist")
    assert info.value.ref_at == ConfigPath.parse("tool.url")


def test_circular_reference_raises():
    src = DictSource(
        {
            ConfigPath.parse("a"): StringValue("@{b}"),
            ConfigPath.parse("b"): StringValue("@{a}"),
        }
    )
    with pytest.raises(CircularRefError) as info:
        ConfigBundle.from_sources([src])
    assert ConfigPath.parse("a") in info.value.cycle
    assert ConfigPath.parse("b") in info.value.cycle


def test_self_reference_raises_circular():
    src = DictSource({ConfigPath.parse("x"): StringValue("@{x}")})
    with pytest.raises(CircularRefError):
        ConfigBundle.from_sources([src])


def test_depth_limit_raises():
    # Цепочка длиннее лимита: $0 → $1 → ... → $N
    n = 5
    values: dict[ConfigPath, ConfigValue] = {
        ConfigPath.parse(f"lvl{i}"): StringValue(f"@{{lvl{i + 1}}}") for i in range(n)
    }
    values[ConfigPath.parse(f"lvl{n}")] = IntValue(0)

    resolver = ReferenceResolver(depth_limit=3)
    with pytest.raises(RefDepthExceededError):
        resolver.resolve(values, dict.fromkeys(values, "dict"))


# --- Origin chain ---


def test_origin_chain_for_direct_value_has_length_one():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {ConfigPath.parse("tool.url"): StringValue("https://example.com")},
                name="dict",
            )
        ]
    )
    chain = bundle.flat.origin_chain_of(ConfigPath.parse("tool.url")).value()
    assert chain == (OriginStep(path=ConfigPath.parse("tool.url"), source="dict"),)


def test_origin_chain_for_reference_records_steps():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("base.url"): StringValue("https://example.com"),
                    ConfigPath.parse("tool.url"): StringValue("@{base.url}"),
                },
                name="src",
            )
        ]
    )
    chain = bundle.flat.origin_chain_of(ConfigPath.parse("tool.url")).value()
    assert chain == (
        OriginStep(path=ConfigPath.parse("tool.url"), source="src"),
        OriginStep(path=ConfigPath.parse("base.url"), source="src"),
    )


def test_origin_of_returns_final_source_in_chain():
    bundle = ConfigBundle.from_sources(
        [
            DictSource(
                {ConfigPath.parse("base.url"): StringValue("u")},
                name="base_src",
                priority=10,
            ),
            DictSource(
                {ConfigPath.parse("tool.url"): StringValue("@{base.url}")},
                name="tool_src",
                priority=20,
            ),
        ]
    )
    # origin_of("tool.url") — финальный источник цепочки = "base_src".
    assert bundle.flat.origin_of(ConfigPath.parse("tool.url")).value() == "base_src"


# --- Resolver-объект напрямую (без bundle) ---


def test_resolver_directly_returns_chain():
    values = {
        ConfigPath.parse("a"): IntValue(1),
        ConfigPath.parse("b"): StringValue("@{a}"),
    }
    origins = {ConfigPath.parse("a"): "src", ConfigPath.parse("b"): "src"}
    resolved_values, resolved_origins = ReferenceResolver().resolve(values, origins)

    assert resolved_values[ConfigPath.parse("a")] == IntValue(1)
    assert resolved_values[ConfigPath.parse("b")] == IntValue(1)
    assert resolved_origins[ConfigPath.parse("a")] == (
        OriginStep(path=ConfigPath.parse("a"), source="src"),
    )
    assert resolved_origins[ConfigPath.parse("b")] == (
        OriginStep(path=ConfigPath.parse("b"), source="src"),
        OriginStep(path=ConfigPath.parse("a"), source="src"),
    )
