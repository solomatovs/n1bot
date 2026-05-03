"""ConfigPath: парсинг, рендер, операции."""

from __future__ import annotations

import pytest
from boba.core import (
    ConfigPath,
    ConfigPathParseError,
    IndexSegment,
    NameSegment,
)


def test_root_parse_and_render():
    p = ConfigPath.parse("$")
    assert p.is_root()
    assert p.render() == "$"


def test_simple_name_path():
    p = ConfigPath.parse("$ext.chromadb")
    assert p == ConfigPath.of(NameSegment("ext"), NameSegment("chromadb"))
    assert p.render() == "$ext.chromadb"


def test_index_segment():
    p = ConfigPath.parse("$models[0]")
    assert p == ConfigPath.of(NameSegment("models"), IndexSegment(0))
    assert p.render() == "$models[0]"


def test_mixed_segments():
    p = ConfigPath.parse("$ext.chromadb.tools.kb_search.params.top_k")
    assert p.render() == "$ext.chromadb.tools.kb_search.params.top_k"


def test_join_with_segment_instances():
    p = ConfigPath.parse("$a").join(NameSegment("b"), IndexSegment(0), NameSegment("c"))
    assert p.render() == "$a.b[0].c"


def test_startswith_and_relative_to():
    parent = ConfigPath.parse("$ext.chromadb")
    child = ConfigPath.parse("$ext.chromadb.tools.kb_search")
    assert child.startswith(parent)
    rel = child.relative_to(parent)
    assert rel.render() == "$tools.kb_search"


def test_relative_to_when_not_under_raises():
    a = ConfigPath.parse("$ext.html")
    b = ConfigPath.parse("$ext.chromadb")
    with pytest.raises(ValueError, match="not under prefix"):
        b.relative_to(a)


def test_invalid_path_no_dollar():
    with pytest.raises(ConfigPathParseError):
        ConfigPath.parse("ext.chromadb")


def test_invalid_segment_name():
    with pytest.raises(ConfigPathParseError):
        ConfigPath.parse("$1bad")  # начинается с цифры


def test_negative_index_rejected():
    with pytest.raises(ConfigPathParseError):
        IndexSegment(-1)
