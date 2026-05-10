"""MarkdownKeys — typed MetadataKey'и, проставляемые MarkdownReader'ом."""

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
