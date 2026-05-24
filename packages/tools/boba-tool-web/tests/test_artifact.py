"""WebArtifact: чистые тесты на path-конструирование/frontmatter/concat-stream."""

from __future__ import annotations

import io

from boba.tool.web.tools._artifact import WebArtifact


def test_relative_path_html_with_path_segments() -> None:
    p = WebArtifact.relative_path(
        "https://docs.python.org/3/library/asyncio.html", as_markdown=False,
    )
    assert p == "docs.python.org/3/library/asyncio.html"


def test_relative_path_markdown_replaces_extension() -> None:
    p = WebArtifact.relative_path(
        "https://docs.python.org/3/library/asyncio.html", as_markdown=True,
    )
    assert p == "docs.python.org/3/library/asyncio.md"


def test_relative_path_root_uses_index() -> None:
    p = WebArtifact.relative_path("https://docs.python.org/", as_markdown=False)
    assert p == "docs.python.org/index.html"


def test_relative_path_strips_query() -> None:
    p = WebArtifact.relative_path(
        "https://api.github.com/x?token=secret&v=1", as_markdown=False,
    )
    assert p == "api.github.com/x.html"
    assert "secret" not in p
    assert "token" not in p


def test_relative_path_sanitizes_unsafe_chars() -> None:
    """Запрещённые FS-символы в path-сегменте заменяются на '_'."""
    p = WebArtifact.relative_path(
        "https://x.example.com/foo/bar<script>?q=1", as_markdown=False,
    )
    assert "<" not in p
    assert ">" not in p
    assert p.startswith("x.example.com/foo/")


def test_relative_path_lowercases_host() -> None:
    p = WebArtifact.relative_path(
        "https://Docs.Python.ORG/3/", as_markdown=False,
    )
    assert p.startswith("docs.python.org/")


def test_html_header_contains_source_url() -> None:
    h = WebArtifact.html_header(url="https://x.example.com/a")
    assert h.startswith(b"<!--")
    assert b"source: https://x.example.com/a" in h
    assert h.endswith(b"-->\n")


def test_md_frontmatter_yaml_format() -> None:
    fm = WebArtifact.md_frontmatter(url="https://x.example.com/a")
    assert fm.startswith("---\n")
    assert "source: https://x.example.com/a" in fm
    assert fm.endswith("---\n")


def test_concat_stream_yields_all_bytes_with_negative_n() -> None:
    s = WebArtifact._ConcatBinaryStream(io.BytesIO(b"head"), io.BytesIO(b"tail"))
    assert s.read(-1) == b"headtail"


def test_concat_stream_chunks_across_streams_with_positive_n() -> None:
    s = WebArtifact._ConcatBinaryStream(
        io.BytesIO(b"abc"), io.BytesIO(b"defg"),
    )
    out = []
    while chunk := s.read(2):
        out.append(chunk)
    assert b"".join(out) == b"abcdefg"
