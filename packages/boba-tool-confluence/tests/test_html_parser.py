"""boba-ext-html-parser: collect_headings + anchor_for + text_between."""

from __future__ import annotations

from boba.html_parser import (
    anchor_for,
    collect_headings,
    parse_html,
    plain_text,
    resolve_anchor,
    text_between,
)


def test_collect_headings_returns_all_six_levels():
    html = "<html><body><h1>A</h1><h2>B</h2><h3>C</h3></body></html>"
    soup = parse_html(html)
    hs = collect_headings(soup)
    assert [h.level for h in hs] == [1, 2, 3]
    assert [h.text for h in hs] == ["A", "B", "C"]
    assert [h.index for h in hs] == [1, 2, 3]


def test_max_depth_limits_levels():
    html = "<html><body><h1>A</h1><h3>B</h3><h6>C</h6></body></html>"
    hs = collect_headings(parse_html(html), max_depth=3)
    assert [h.level for h in hs] == [1, 3]


def test_anchor_from_html_id():
    html = '<html><body><h1 id="intro">Intro</h1></body></html>'
    h = collect_headings(parse_html(html))[0]
    assert h.anchor == "intro"
    assert anchor_for(h) == "intro"


def test_anchor_fallback_to_idx_when_no_id():
    html = "<html><body><h1>One</h1><h1>Two</h1></body></html>"
    hs = collect_headings(parse_html(html))
    assert hs[0].anchor is None
    assert anchor_for(hs[0]) == "idx:1"
    assert anchor_for(hs[1]) == "idx:2"


def test_resolve_anchor_by_id_or_idx():
    html = '<html><body><h1 id="x">A</h1><h2>B</h2></body></html>'
    hs = collect_headings(parse_html(html))
    assert resolve_anchor(hs, "x") is hs[0]
    assert resolve_anchor(hs, "#x") is hs[0]
    assert resolve_anchor(hs, "idx:2") is hs[1]
    assert resolve_anchor(hs, "missing") is None


def test_text_between_joins_text_until_next_heading():
    html = """
    <html><body>
      <h1>One</h1><p>alpha</p><p>beta</p>
      <h1>Two</h1><p>gamma</p>
    </body></html>
    """
    hs = collect_headings(parse_html(html))
    between_first = text_between(hs[0].tag, hs[1].tag)
    assert "alpha" in between_first
    assert "beta" in between_first
    assert "gamma" not in between_first


def test_text_between_skips_heading_text_itself():
    """Текст самого heading'а не должен попадать в text_between."""
    html = "<html><body><h1>Title</h1><p>body</p></body></html>"
    hs = collect_headings(parse_html(html))
    between = text_between(hs[0].tag, None)
    assert "Title" not in between
    assert "body" in between


def test_plain_text_full_subtree():
    html = "<html><body><p>one</p><p>two</p></body></html>"
    soup = parse_html(html)
    body = soup.body
    assert body is not None
    text = plain_text(body)
    assert "one" in text
    assert "two" in text


def test_custom_anchor_extractor():
    """Диалект может переопределить anchor_extractor."""
    html = '<html><body><h1 data-anchor="custom">A</h1></body></html>'
    soup = parse_html(html)

    def my_anchor(tag) -> str | None:
        v = tag.get("data-anchor")
        if isinstance(v, list):
            return v[0] if v else None
        return str(v) if v else None

    hs = collect_headings(soup, anchor_extractor=my_anchor)
    assert hs[0].anchor == "custom"


def test_custom_text_extractor():
    """Диалект может переопределить text_extractor (например исключать спец-теги)."""
    html = "<html><body><h1>visible<skip>SKIP</skip></h1></body></html>"
    soup = parse_html(html)

    def my_text(tag):
        # Берём только текст, не находящийся внутри <skip>
        from bs4.element import NavigableString
        parts = []
        for el in tag.descendants:
            if not isinstance(el, NavigableString):
                continue
            if any(getattr(p, "name", "") == "skip" for p in el.parents):
                continue
            parts.append(str(el))
        return " ".join(" ".join(parts).split())

    hs = collect_headings(soup, text_extractor=my_text)
    assert "visible" in hs[0].text
    assert "SKIP" not in hs[0].text
