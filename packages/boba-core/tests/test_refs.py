"""ReferenceResolver: разрешение `@{path}`-ссылок в FlatConfig."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from boba.config.bundle import ConfigBundle
from boba.config.path import ConfigPath, ConfigSource
from boba.config.refs import (
    CircularRefError,
    OriginStep,
    RefDepthExceededError,
    ReferenceResolver,
    UnresolvedRefError,
)
from boba.value import (
    BoolValue,
    ConfigValue,
    IntValue,
    StringValue,
)


class _DictSource(ConfigSource):
    def __init__(
        self,
        values: Mapping[ConfigPath, ConfigValue],
        *,
        name: str = "dict",
        priority: int = 100,
    ) -> None:
        self._v = values
        self._name = name
        self._priority = priority

    def name(self) -> str:
        return self._name

    def priority(self) -> int:
        return self._priority

    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        return dict(self._v)


def _path(s: str) -> ConfigPath:
    return ConfigPath.parse(s)


# --- Полная замена ---


def test_full_replacement_string_value():
    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    _path("base.url"): StringValue("https://example.com"),
                    _path("tool.search.url"): StringValue("@{base.url}"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(_path("tool.search.url")).value() == StringValue(
        "https://example.com"
    )


def test_reference_preserves_non_string_value_type():
    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    _path("base.timeout"): IntValue(30),
                    _path("tool.search.timeout"): StringValue("@{base.timeout}"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(_path("tool.search.timeout")).value() == IntValue(30)


def test_reference_preserves_bool_value_type():
    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    _path("base.flag"): BoolValue(True),
                    _path("tool.flag"): StringValue("@{base.flag}"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(_path("tool.flag")).value() == BoolValue(True)


def test_partial_template_is_not_a_reference():
    """`prefix-@{X}-suffix` — обычная строка, не интерполируется."""
    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    _path("base.host"): StringValue("example.com"),
                    _path("tool.url"): StringValue("https://@{base.host}/api"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(_path("tool.url")).value() == StringValue(
        "https://@{base.host}/api"
    )


def test_non_reference_value_unchanged():
    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    _path("tool.name"): StringValue("plain string"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(_path("tool.name")).value() == StringValue(
        "plain string"
    )


# --- Цепочки ---


def test_chain_of_references_resolves_to_final():
    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    _path("root.value"): IntValue(42),
                    _path("mid.value"): StringValue("@{root.value}"),
                    _path("leaf.value"): StringValue("@{mid.value}"),
                }
            )
        ]
    )
    assert bundle.flat.lookup(_path("leaf.value")).value() == IntValue(42)


# --- Ошибки ---


def test_unresolved_reference_raises():
    src = _DictSource(
        {_path("tool.url"): StringValue("@{base.does_not_exist}")},
    )
    with pytest.raises(UnresolvedRefError) as info:
        ConfigBundle.from_sources([src])
    assert info.value.target == _path("base.does_not_exist")
    assert info.value.ref_at == _path("tool.url")


def test_circular_reference_raises():
    src = _DictSource(
        {
            _path("a"): StringValue("@{b}"),
            _path("b"): StringValue("@{a}"),
        }
    )
    with pytest.raises(CircularRefError) as info:
        ConfigBundle.from_sources([src])
    assert _path("a") in info.value.cycle
    assert _path("b") in info.value.cycle


def test_self_reference_raises_circular():
    src = _DictSource({_path("x"): StringValue("@{x}")})
    with pytest.raises(CircularRefError):
        ConfigBundle.from_sources([src])


def test_depth_limit_raises():
    # Цепочка длиннее лимита: $0 → $1 → ... → $N
    n = 5
    values: dict[ConfigPath, ConfigValue] = {
        _path(f"lvl{i}"): StringValue(f"@{{lvl{i + 1}}}") for i in range(n)
    }
    values[_path(f"lvl{n}")] = IntValue(0)

    resolver = ReferenceResolver(depth_limit=3)
    with pytest.raises(RefDepthExceededError):
        resolver.resolve(values, dict.fromkeys(values, "dict"))


# --- Origin chain ---


def test_origin_chain_for_direct_value_has_length_one():
    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {_path("tool.url"): StringValue("https://example.com")},
                name="dict",
            )
        ]
    )
    chain = bundle.flat.origin_chain_of(_path("tool.url")).value()
    assert chain == (OriginStep(path=_path("tool.url"), source="dict"),)


def test_origin_chain_for_reference_records_steps():
    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {
                    _path("base.url"): StringValue("https://example.com"),
                    _path("tool.url"): StringValue("@{base.url}"),
                },
                name="src",
            )
        ]
    )
    chain = bundle.flat.origin_chain_of(_path("tool.url")).value()
    assert chain == (
        OriginStep(path=_path("tool.url"), source="src"),
        OriginStep(path=_path("base.url"), source="src"),
    )


def test_origin_of_returns_final_source_in_chain():
    bundle = ConfigBundle.from_sources(
        [
            _DictSource(
                {_path("base.url"): StringValue("u")},
                name="base_src",
                priority=10,
            ),
            _DictSource(
                {_path("tool.url"): StringValue("@{base.url}")},
                name="tool_src",
                priority=20,
            ),
        ]
    )
    # origin_of("tool.url") — финальный источник цепочки = "base_src".
    assert bundle.flat.origin_of(_path("tool.url")).value() == "base_src"


# --- Resolver-объект напрямую (без bundle) ---


def test_resolver_directly_returns_chain():
    values = {
        _path("a"): IntValue(1),
        _path("b"): StringValue("@{a}"),
    }
    origins = {_path("a"): "src", _path("b"): "src"}
    resolved_values, resolved_origins = ReferenceResolver().resolve(values, origins)

    assert resolved_values[_path("a")] == IntValue(1)
    assert resolved_values[_path("b")] == IntValue(1)
    assert resolved_origins[_path("a")] == (
        OriginStep(path=_path("a"), source="src"),
    )
    assert resolved_origins[_path("b")] == (
        OriginStep(path=_path("b"), source="src"),
        OriginStep(path=_path("a"), source="src"),
    )
