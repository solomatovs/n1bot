"""FlatConfig + merger."""

from __future__ import annotations

from boba.config.bundle import ConfigBundle
from boba.config.flat import FlatConfig
from boba.config.path import ConfigPath, NameSegment
from boba.config.source.dict_source import DictSource
from boba.value import IntValue, StringValue


def test_lookup_found_and_not_found():
    from boba.config.refs import OriginStep  # noqa: PLC0415

    p = ConfigPath.parse("a")
    flat = FlatConfig(
        values={p: IntValue(1)},
        origins={p: (OriginStep(path=p, source="test"),)},
    )
    assert flat.lookup(p).value() == IntValue(1)
    assert not flat.lookup(ConfigPath.parse("missing")).is_found()


def test_merge_priority_last_wins():
    p_a = ConfigPath.parse("x")
    low = DictSource({p_a: StringValue("from-low")}, name="low", priority=100)
    high = DictSource({p_a: StringValue("from-high")}, name="high", priority=200)
    flat = ConfigBundle.from_sources([low, high]).flat
    assert flat.lookup(p_a).value() == StringValue("from-high")
    assert flat.origin_of(p_a).value() == "high"


def test_keys_under_and_subtree():
    paths = {
        ConfigPath.parse("ext.html.enabled"): StringValue("true"),
        ConfigPath.parse("ext.html.tools.html_outline.enabled"): StringValue("true"),
        ConfigPath.parse("ext.chromadb.enabled"): StringValue("false"),
    }
    src = DictSource(paths, name="toml", priority=100)
    flat = ConfigBundle.from_sources([src]).flat
    sub = flat.subtree(ConfigPath.parse("ext.html"))
    assert ConfigPath.parse("ext.html.enabled") in sub
    assert ConfigPath.parse("ext.html.tools.html_outline.enabled") in sub
    assert ConfigPath.parse("ext.chromadb.enabled") not in sub


def test_child_segments_returns_unique_first_segments():
    paths = {
        ConfigPath.parse("ext.html.tools.html_outline.enabled"): StringValue("a"),
        ConfigPath.parse("ext.html.tools.html_outline.description"): StringValue("b"),
        ConfigPath.parse("ext.html.tools.html_section.enabled"): StringValue("c"),
    }
    src = DictSource(paths, name="toml", priority=100)
    flat = ConfigBundle.from_sources([src]).flat
    children = list(flat.child_segments(ConfigPath.parse("ext.html.tools")))
    assert NameSegment("html_outline") in children
    assert NameSegment("html_section") in children
    assert len(children) == 2
