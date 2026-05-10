"""HtmlKeys — typed `MetadataKey`-и, проставляемые HTML-Reader'ом и
HTML-специфичными Section'ами через `to_chunk_metadata()`.
"""

from __future__ import annotations

from typing import ClassVar

from boba.indexing import MetadataKey

__all__ = ["HtmlKeys"]


class HtmlKeys:
    """HTML-специфичные ключи metadata."""

    HEADING_LEVEL: ClassVar[MetadataKey[int]] = MetadataKey(
        name="reader.html.heading_level",
        decode=int,
        encode=str,
    )
    HEADING_TEXT: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.html.heading_text",
        decode=str,
        encode=str,
    )
    CODE_LANGUAGE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.html.code_language",
        decode=str,
        encode=str,
    )
    LIST_ORDERED: ClassVar[MetadataKey[bool]] = MetadataKey(
        name="reader.html.list_ordered",
        decode=lambda s: s == "1",
        encode=lambda v: "1" if v else "0",
    )
    TABLE_HEADER: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.html.table_header",
        decode=str,
        encode=str,
    )
