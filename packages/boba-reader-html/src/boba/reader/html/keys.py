"""HtmlKeys — MetadataKey, проставляемые HtmlReader"""

from __future__ import annotations

from typing import ClassVar

from boba.indexing import MetadataKey

__all__ = ["HtmlKeys"]


class HtmlKeys:
    """HTML-специфичные ключи metadata"""

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
