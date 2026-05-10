"""MarkdownKeys — typed MetadataKey'и, проставляемые markdown-Reader'ом и
markdown-специфичными Section'ами при `to_chunk_metadata()`.
"""

from __future__ import annotations

from typing import ClassVar

from boba.indexing import MetadataKey

__all__ = ["MarkdownKeys"]


class MarkdownKeys:
    """Markdown-специфичные ключи metadata."""

    HEADING_LEVEL: ClassVar[MetadataKey[int]] = MetadataKey(
        name="reader.markdown.heading_level",
        decode=int,
        encode=str,
    )
    HEADING_TEXT: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.markdown.heading_text",
        decode=str,
        encode=str,
    )
    CODE_LANGUAGE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.markdown.code_language",
        decode=str,
        encode=str,
    )
    LIST_ORDERED: ClassVar[MetadataKey[bool]] = MetadataKey(
        name="reader.markdown.list_ordered",
        decode=lambda s: s == "1",
        encode=lambda v: "1" if v else "0",
    )
    TABLE_HEADER: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.markdown.table_header",
        decode=str,
        encode=str,
    )
