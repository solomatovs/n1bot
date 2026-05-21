"""Unit-тесты HtmlChunkParser.

Без внешних сервисов: парсер чисто in-memory, проверяем границы
чанков, boilerplate-фильтр, canonical URL, heading_path, рендер
структурных секций и size-cap.
"""

from __future__ import annotations

import pytest

from boba.indexing.metadata import ReaderKeys
from boba.indexing.sections import SourceId
from boba.tool.postgres.html_chunk import (
    HEADING_PATH_KEY,
    SOURCE_URL_KEY,
    HtmlChunkParser,
)


def _wire(parser_output: object) -> dict[str, str]:
    """Удобный shortcut: chunk.metadata.to_wire() → dict для проверок."""
    return dict(parser_output.metadata.to_wire())  # type: ignore[attr-defined]


# --------------------------------------------------------------------- #
# Базовый split по H1/H2                                                #
# --------------------------------------------------------------------- #


def test_split_h1_h2_yields_chunk_per_divider() -> None:
    """H1 и H2 — оба разделители: получаем один чанк на каждый."""
    html = """
    <h1 id="intro">Intro</h1>
    <p>Body 1.</p>
    <h2 id="setup">Setup</h2>
    <p>Body 2.</p>
    <h2 id="run">Run</h2>
    <p>Body 3.</p>
    """
    parser = HtmlChunkParser()
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("test.html")),
    )
    assert len(chunks) == 3
    assert [c.metadata.to_wire()["anchor"] for c in chunks] == [
        "intro", "setup", "run",
    ]
    assert chunks[0].format_content.startswith("# Intro")
    assert chunks[1].format_content.startswith("# Setup")
    assert chunks[2].format_content.startswith("# Run")


def test_sub_headings_stay_in_body_as_markdown() -> None:
    """H3 в split_levels={1,2} остаётся внутри H2-чанка как `### Title`."""
    html = """
    <h1>Doc</h1>
    <h2 id="setup">Setup</h2>
    <p>Intro paragraph.</p>
    <h3>From repo</h3>
    <p>Clone first.</p>
    <h3>From pip</h3>
    <p>Or install.</p>
    """
    parser = HtmlChunkParser()
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("doc.html")),
    )
    setup_chunk = next(c for c in chunks if "Setup" in c.format_content)
    assert "### From repo" in setup_chunk.format_content
    assert "### From pip" in setup_chunk.format_content


def test_split_levels_custom() -> None:
    """split_levels={1} — H2 не режет, остаётся как sub-heading."""
    html = """
    <h1 id="a">A</h1>
    <p>aa</p>
    <h2>A1</h2>
    <p>a1</p>
    <h1 id="b">B</h1>
    <p>bb</p>
    """
    parser = HtmlChunkParser(split_levels=frozenset({1}))
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("test.html")),
    )
    assert len(chunks) == 2
    a_chunk = next(c for c in chunks if c.metadata.to_wire()["anchor"] == "a")
    assert "## A1" in a_chunk.format_content


def test_preamble_before_first_heading_skipped() -> None:
    """Параграфы до первого split-heading'а молча пропускаются."""
    html = """
    <p>Preamble paragraph.</p>
    <p>Another preamble.</p>
    <h1 id="real">Real</h1>
    <p>Body.</p>
    """
    parser = HtmlChunkParser()
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("test.html")),
    )
    assert len(chunks) == 1
    assert "Preamble" not in chunks[0].format_content


# --------------------------------------------------------------------- #
# Boilerplate filter                                                    #
# --------------------------------------------------------------------- #


def test_boilerplate_stripped_before_section_parsing() -> None:
    """script/style/nav/footer/aside/header вырезаются — текст из них
    не попадает ни в один чанк."""
    html = """
    <html><head><title>T</title></head><body>
    <nav><a href="/x">NavLink should not leak</a></nav>
    <header>HeaderText leak</header>
    <h1 id="real">Real Section</h1>
    <p>Real body.</p>
    <aside>SidebarJunk</aside>
    <script>console.log("JS code");</script>
    <style>body{color:red}</style>
    <footer>Copyright FooterJunk</footer>
    </body></html>
    """
    parser = HtmlChunkParser()
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("page.html")),
    )
    assert len(chunks) == 1
    body = chunks[0].format_content
    for noise in (
        "NavLink", "HeaderText", "SidebarJunk", "console.log",
        "color:red", "FooterJunk",
    ):
        assert noise not in body, f"boilerplate leaked: {noise!r} in chunk"


# --------------------------------------------------------------------- #
# Canonical URL extraction                                              #
# --------------------------------------------------------------------- #


def test_canonical_url_from_link_rel() -> None:
    html = """
    <html><head>
        <link rel="canonical" href="https://example.com/page" />
    </head><body>
    <h1 id="x">X</h1><p>p</p>
    </body></html>
    """
    parser = HtmlChunkParser()
    chunk = next(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    assert _wire(chunk)[SOURCE_URL_KEY.name] == "https://example.com/page"


def test_canonical_url_fallback_to_og() -> None:
    """`<meta property="og:url">` используется, если canonical отсутствует."""
    html = """
    <html><head>
        <meta property="og:url" content="https://example.com/og">
    </head><body>
    <h1 id="x">X</h1><p>p</p>
    </body></html>
    """
    parser = HtmlChunkParser()
    chunk = next(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    assert _wire(chunk)[SOURCE_URL_KEY.name] == "https://example.com/og"


def test_canonical_disabled_by_flag() -> None:
    """`extract_canonical_url=False` — URL не пишется даже если есть в HTML."""
    html = """
    <link rel="canonical" href="https://example.com/" />
    <h1>X</h1><p>p</p>
    """
    parser = HtmlChunkParser(extract_canonical_url=False)
    chunk = next(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    assert SOURCE_URL_KEY.name not in _wire(chunk)


def test_no_canonical_means_empty() -> None:
    """Если canonical/OG отсутствует — source_url-ключа просто нет в metadata."""
    html = "<h1>X</h1><p>p</p>"
    parser = HtmlChunkParser()
    chunk = next(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    assert SOURCE_URL_KEY.name not in _wire(chunk)


# --------------------------------------------------------------------- #
# Heading path                                                          #
# --------------------------------------------------------------------- #


def test_heading_path_includes_full_chain() -> None:
    """`heading_path` собирается через ` > ` от H1 до текущего разделителя."""
    html = """
    <h1>Top</h1>
    <h2 id="a">A</h2>
    <p>p</p>
    <h2 id="b">B</h2>
    <p>p</p>
    """
    parser = HtmlChunkParser()
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("p.html")),
    )
    # chunk per H1, H2 A, H2 B
    paths = [_wire(c).get(HEADING_PATH_KEY.name) for c in chunks]
    assert paths == ["Top", "Top > A", "Top > B"]


def test_heading_path_resets_on_higher_level() -> None:
    """Новый H1 сбрасывает H2/H3 в стеке."""
    html = """
    <h1>One</h1>
    <h2 id="a">A</h2>
    <p>p</p>
    <h1>Two</h1>
    <h2 id="b">B</h2>
    <p>p</p>
    """
    parser = HtmlChunkParser()
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("p.html")),
    )
    a = next(c for c in chunks if _wire(c).get("anchor") == "a")
    b = next(c for c in chunks if _wire(c).get("anchor") == "b")
    assert _wire(a)[HEADING_PATH_KEY.name] == "One > A"
    assert _wire(b)[HEADING_PATH_KEY.name] == "Two > B"


# --------------------------------------------------------------------- #
# Structured rendering: code / list / table                              #
# --------------------------------------------------------------------- #


def test_code_block_renders_as_fenced_markdown() -> None:
    html = """
    <h1>X</h1>
    <pre><code class="language-python">print("hi")
print("bye")</code></pre>
    """
    parser = HtmlChunkParser()
    chunk = next(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    assert '```python\nprint("hi")\nprint("bye")\n```' in chunk.format_content


def test_list_renders_as_dash_bullets() -> None:
    html = """
    <h1>X</h1>
    <ul><li>One</li><li>Two</li><li>Three</li></ul>
    """
    parser = HtmlChunkParser()
    chunk = next(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    assert "- One" in chunk.format_content
    assert "- Two" in chunk.format_content
    assert "- Three" in chunk.format_content


def test_table_renders_as_markdown_table() -> None:
    html = """
    <h1>X</h1>
    <table>
      <tr><th>Col1</th><th>Col2</th></tr>
      <tr><td>a</td><td>b</td></tr>
      <tr><td>c</td><td>d</td></tr>
    </table>
    """
    parser = HtmlChunkParser()
    chunk = next(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    assert "| Col1 | Col2 |" in chunk.format_content
    assert "|---|---|" in chunk.format_content
    assert "| a | b |" in chunk.format_content
    assert "| c | d |" in chunk.format_content


# --------------------------------------------------------------------- #
# Size cap                                                              #
# --------------------------------------------------------------------- #


def test_size_cap_splits_long_body_at_paragraph_boundaries() -> None:
    """body > max_chunk_chars → разбивается по `<p>` границам."""
    long_p = "<p>" + ("слово " * 200) + "</p>"  # ~1200+ символов
    html = f"<h1>X</h1>{long_p}{long_p}{long_p}"
    parser = HtmlChunkParser(max_chunk_chars=600)
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    # Под cap должно влезть по одному p в чанк (или около того).
    assert len(chunks) >= 3, f"expected ≥3 sub-chunks, got {len(chunks)}"
    # Все sub-chunks наследуют heading и доменный page_title.
    for c in chunks:
        assert c.format_content.startswith("# X")
        assert _wire(c).get(ReaderKeys.PAGE_TITLE.name) == "X"
    # chunk_index монотонно растёт.
    indices = [c.chunk_index for c in chunks]
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)


def test_size_cap_disabled_keeps_single_chunk() -> None:
    """max_chunk_chars=0 — без разбивки, длинный body остаётся одним чанком."""
    long_p = "<p>" + ("слово " * 500) + "</p>"
    html = f"<h1>X</h1>{long_p}{long_p}"
    parser = HtmlChunkParser(max_chunk_chars=0)
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    assert len(chunks) == 1


# --------------------------------------------------------------------- #
# Min chunk filter + edge cases                                          #
# --------------------------------------------------------------------- #


def test_min_chunk_chars_filters_short_chunks() -> None:
    """Чанки короче min_chunk_chars пропускаются."""
    html = """
    <h1 id="big">Big section</h1>
    <p>Long body with enough content to pass the threshold easily.</p>
    <h1 id="empty">x</h1>
    <h1 id="big2">Big again</h1>
    <p>Another long body that passes the threshold.</p>
    """
    parser = HtmlChunkParser(min_chunk_chars=30)
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    anchors = [_wire(c)["anchor"] for c in chunks]
    assert "empty" not in anchors  # отфильтрован
    assert "big" in anchors and "big2" in anchors


def test_empty_html_yields_no_chunks() -> None:
    parser = HtmlChunkParser()
    chunks = list(
        parser.build_chunks_from_text("", source_id=SourceId("empty.html")),
    )
    assert chunks == []


def test_html_without_headings_yields_no_chunks() -> None:
    """Без heading'ов уровня split_levels — нет разделителей, нечем резать."""
    html = "<p>Just text.</p><p>More text.</p>"
    parser = HtmlChunkParser()
    chunks = list(
        parser.build_chunks_from_text(html, source_id=SourceId("flat.html")),
    )
    assert chunks == []


# --------------------------------------------------------------------- #
# Construction guards                                                   #
# --------------------------------------------------------------------- #


def test_empty_split_levels_rejected() -> None:
    with pytest.raises(ValueError, match="split_levels"):
        HtmlChunkParser(split_levels=frozenset())


def test_negative_chunk_chars_rejected() -> None:
    with pytest.raises(ValueError, match="min_chunk_chars"):
        HtmlChunkParser(min_chunk_chars=-1)
    with pytest.raises(ValueError, match="max_chunk_chars"):
        HtmlChunkParser(max_chunk_chars=-1)


# --------------------------------------------------------------------- #
# page_title fallback to <title>                                        #
# --------------------------------------------------------------------- #


def test_page_title_falls_back_to_title_tag_when_no_h1() -> None:
    """Если разделитель — H2 (нет H1), берём `<title>` из head."""
    html = """
    <html><head><title>Fallback Title</title></head><body>
    <h2 id="a">A</h2>
    <p>p</p>
    </body></html>
    """
    parser = HtmlChunkParser()
    chunk = next(
        parser.build_chunks_from_text(html, source_id=SourceId("x.html")),
    )
    assert _wire(chunk).get(ReaderKeys.PAGE_TITLE.name) == "Fallback Title"
