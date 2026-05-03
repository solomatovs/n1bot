"""FlatConfig + merger."""

from __future__ import annotations

from collections.abc import Mapping

from boba.core import (
    ConfigBundle,
    ConfigPath,
    ConfigSource,
    ConfigValue,
    FlatConfig,
    IntValue,
    NameSegment,
    StringValue,
)


class _DictSource(ConfigSource):
    def __init__(
        self,
        name: str,
        priority: int,
        values: Mapping[ConfigPath, ConfigValue],
    ) -> None:
        self._n = name
        self._p = priority
        self._v = values

    def name(self) -> str:
        return self._n

    def priority(self) -> int:
        return self._p

    def load(self) -> Mapping[ConfigPath, ConfigValue]:
        return dict(self._v)


def test_lookup_found_and_not_found():
    flat = FlatConfig(
        values={ConfigPath.parse("$a"): IntValue(1)},
        origins={ConfigPath.parse("$a"): "test"},
    )
    assert flat.lookup(ConfigPath.parse("$a")).value() == IntValue(1)
    assert not flat.lookup(ConfigPath.parse("$missing")).is_found()


def test_merge_priority_last_wins():
    p_a = ConfigPath.parse("$x")
    low = _DictSource("low", 100, {p_a: StringValue("from-low")})
    high = _DictSource("high", 200, {p_a: StringValue("from-high")})
    flat = ConfigBundle.from_sources([low, high]).flat
    assert flat.lookup(p_a).value() == StringValue("from-high")
    assert flat.origin_of(p_a).value() == "high"


def test_keys_under_and_subtree():
    paths = {
        ConfigPath.parse("$ext.html.enabled"): StringValue("true"),
        ConfigPath.parse("$ext.html.tools.html_outline.enabled"): StringValue("true"),
        ConfigPath.parse("$ext.chromadb.enabled"): StringValue("false"),
    }
    src = _DictSource("toml", 100, paths)
    flat = ConfigBundle.from_sources([src]).flat
    sub = flat.subtree(ConfigPath.parse("$ext.html"))
    assert ConfigPath.parse("$ext.html.enabled") in sub
    assert ConfigPath.parse("$ext.html.tools.html_outline.enabled") in sub
    assert ConfigPath.parse("$ext.chromadb.enabled") not in sub


def test_child_segments_returns_unique_first_segments():
    paths = {
        ConfigPath.parse("$ext.html.tools.html_outline.enabled"): StringValue("a"),
        ConfigPath.parse("$ext.html.tools.html_outline.description"): StringValue("b"),
        ConfigPath.parse("$ext.html.tools.html_section.enabled"): StringValue("c"),
    }
    src = _DictSource("toml", 100, paths)
    flat = ConfigBundle.from_sources([src]).flat
    children = list(flat.child_segments(ConfigPath.parse("$ext.html.tools")))
    assert NameSegment("html_outline") in children
    assert NameSegment("html_section") in children
    assert len(children) == 2
