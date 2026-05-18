"""Unit-тесты `MdChunkParser`."""

from __future__ import annotations

import pytest

from boba.indexing.chunks import ChunkKeys
from boba.indexing.metadata import ReaderKeys
from boba.indexing.sections import SourceId
from boba.tool.chromadb.md_chunk import MdChunkParser


@pytest.fixture
def parser() -> MdChunkParser:
    return MdChunkParser()


def test_parse_full_format(parser: MdChunkParser) -> None:
    text = """# Refund Policy

**tags:** payments, policy
**source:** https://wiki.example.com/payments/refund
**anchor:** refund-policy

---

We refund within 14 days. The body content goes here.

Multiple paragraphs are fine.
"""
    parsed = parser.parse(text)

    assert parsed.title == "Refund Policy"
    assert parsed.tags == frozenset({"payments", "policy"})
    assert parsed.metadata == {
        "source": "https://wiki.example.com/payments/refund",
        "anchor": "refund-policy",
    }
    assert parsed.body.startswith("We refund within 14 days.")
    assert "Multiple paragraphs are fine." in parsed.body


def test_parse_no_metadata_block(parser: MdChunkParser) -> None:
    text = """# Standalone Note

Just a plain body without a metadata block or `---` separator.
"""
    parsed = parser.parse(text)

    assert parsed.title == "Standalone Note"
    assert parsed.tags == frozenset()
    assert parsed.metadata == {}
    assert parsed.body.startswith("Just a plain body")


def test_parse_unknown_metadata_keys_prefixed(parser: MdChunkParser) -> None:
    """Неизвестные ключи метаданных оседают в metadata через build_chunk."""
    text = """# Custom Doc

**tags:** custom
**source:** https://example.com
**owner:** alice
**version:** 2

---

Body.
"""
    parsed = parser.parse(text)
    assert parsed.metadata["owner"] == "alice"
    assert parsed.metadata["version"] == "2"

    chunk = parser.build_chunk_from_text(
        text, source_id=SourceId("custom.md"),
    )
    wire = chunk.metadata.to_wire()
    assert wire["md.owner"] == "alice"
    assert wire["md.version"] == "2"


def test_parse_rejects_missing_h1(parser: MdChunkParser) -> None:
    text = """## Subheading first

Body without H1.
"""
    with pytest.raises(ValueError, match="must be H1"):
        parser.parse(text)


def test_parse_rejects_empty_file(parser: MdChunkParser) -> None:
    with pytest.raises(ValueError, match="empty"):
        parser.parse("")
    with pytest.raises(ValueError, match="empty"):
        parser.parse("   \n\n  ")


def test_parse_russian_text(parser: MdChunkParser) -> None:
    text = """# Возврат денег

**tags:** платежи, политика
**source:** https://wiki.example.com/ru/refund
**anchor:** vozvrat

---

Возврат возможен в течение 14 дней.
"""
    parsed = parser.parse(text)
    assert parsed.title == "Возврат денег"
    assert parsed.tags == frozenset({"платежи", "политика"})
    assert "Возврат возможен" in parsed.body


def test_build_chunk_source_url_and_anchor_wire_keys(
    parser: MdChunkParser,
) -> None:
    """source_url + anchor пишутся плоскими ключами (для kb_search link-builder).

    kb_search._build_link читает 'source_url' и 'anchor' напрямую из
    chroma-metadata — поэтому их пишем без dotted-prefix. anchor дублируется
    как chunk.anchor (для типизированного read-back через ChunkKeys.ANCHOR).
    """
    text = """# T

**source:** https://example.com/page
**anchor:** sec-1

---

body
"""
    chunk = parser.build_chunk_from_text(text, source_id=SourceId("doc.md"))
    wire = chunk.metadata.to_wire()
    assert wire["source_url"] == "https://example.com/page"
    assert wire["anchor"] == "sec-1"
    assert wire[ChunkKeys.ANCHOR.name] == "sec-1"
    assert wire[ReaderKeys.DOC_TYPE.name] == "markdown"
    assert wire[ReaderKeys.PAGE_TITLE.name] == "T"


def test_build_chunk_stable_chunk_id_per_source(
    parser: MdChunkParser,
) -> None:
    """chunk_id детерминируется ТОЛЬКО source_id: меняем тело — id не меняется.

    Это нужно для idempotent upsert: тот же файл при изменении контента
    должен ЗАМЕНИТЬ старый чанк, а не создать второй с другим id.
    """
    a = parser.build_chunk_from_text(
        "# Title\n\nA", source_id=SourceId("same.md"),
    )
    b = parser.build_chunk_from_text(
        "# Title\n\nB", source_id=SourceId("same.md"),
    )
    assert a.chunk_id == b.chunk_id
    # content_hash должен отличаться (детектит изменение)
    assert a.content_hash is not None
    assert b.content_hash is not None
    assert a.content_hash.to_wire() != b.content_hash.to_wire()


def test_build_chunk_different_source_different_id(
    parser: MdChunkParser,
) -> None:
    a = parser.build_chunk_from_text(
        "# T\n\nBody", source_id=SourceId("a.md"),
    )
    b = parser.build_chunk_from_text(
        "# T\n\nBody", source_id=SourceId("b.md"),
    )
    assert a.chunk_id != b.chunk_id


def test_format_content_combines_title_and_body(
    parser: MdChunkParser,
) -> None:
    """format_content (то, что эмбедится) = H1 + body — для семантического матча."""
    text = """# Searchable Title

**tags:** x

---

Body content.
"""
    chunk = parser.build_chunk_from_text(text, source_id=SourceId("s.md"))
    assert chunk.format_content.startswith("# Searchable Title")
    assert "Body content." in chunk.format_content
    # raw_content = весь оригинальный файл (для citation).
    assert chunk.raw_content == text


def test_tags_split_and_trim(parser: MdChunkParser) -> None:
    text = """# T

**tags:**   alpha  ,beta,   gamma,,

---

body
"""
    parsed = parser.parse(text)
    assert parsed.tags == frozenset({"alpha", "beta", "gamma"})


def test_no_hr_means_no_metadata_extracted(parser: MdChunkParser) -> None:
    """Без `---` блок `**key:** value` считается частью body, не метаданными.

    Защита от ложно-положительной интерпретации: оператор может писать
    `**Note:** ...` в обычном тексте — без HR это body.
    """
    text = """# T

**tags:** ignored, when, no, hr
**source:** https://example.com

Some body — looks like metadata but no `---` separator.
"""
    parsed = parser.parse(text)
    assert parsed.tags == frozenset()
    assert parsed.metadata == {}
    assert "ignored, when, no, hr" in parsed.body
