"""
ConfluenceKeys — MetadataKey, проставляемые Confluence
"""

from __future__ import annotations

from typing import ClassVar

from boba.indexing import MetadataKey

__all__ = ["ConfluenceKeys"]


class ConfluenceKeys:
    """Confluence-специфичные ключи metadata."""

    PAGE_ID: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.page_id",
        decode=str,
        encode=str,
    )
    HOST: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.host",
        decode=str,
        encode=str,
    )
    VERSION: ClassVar[MetadataKey[int]] = MetadataKey(
        name="confluence.version",
        decode=int,
        encode=str,
    )
    SPACE_KEY: ClassVar[MetadataKey[str]] = MetadataKey(
        name="confluence.space_key",
        decode=str,
        encode=str,
    )
