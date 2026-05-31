"""KbDocKeys — типизированные MetadataKey'и формата KB-документа.

`source_url` пишется без namespace-префикса намеренно: kb_search_* tools и UI
ожидают его как top-level ключ для построения deep-link'ов в выдаче.
Остальные header-ключи живут под `reader.kbdoc.*`.
"""

from __future__ import annotations

from typing import ClassVar

from boba.indexing import MetadataKey

__all__ = ["KbDocKeys"]


class KbDocKeys:
    """Ключи, проставляемые `KbDocReader` из header-блока."""

    SOURCE_URL: ClassVar[MetadataKey[str]] = MetadataKey(
        name="source_url",
        decode=str,
        encode=str,
    )
    """Canonical URL источника — для citation/deep-link в search."""

    PAGE_ID: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.kbdoc.page_id",
        decode=str,
        encode=str,
    )
    """ID исходной страницы (например Confluence pageId) — для re-fetch."""

    SPACE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.kbdoc.space",
        decode=str,
        encode=str,
    )
    """Space/namespace источника — для группировки и llm.space."""

    CUSTOM_PREFIX: ClassVar[str] = "reader.kbdoc."
    """Префикс для нераспознанных header-ключей: `reader.kbdoc.{name}`."""
