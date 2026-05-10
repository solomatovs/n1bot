"""HttpKeys — typed MetadataKey'и, проставляемые HTTP-стадиями.

Дополняет `TransportKeys` (`ETAG`/`CONTENT_TYPE`/`MTIME`) HTTP-специфичными
полями: last-modified header, response status code.
"""

from __future__ import annotations

from typing import ClassVar

from boba.indexing import MetadataKey

__all__ = ["HttpKeys"]


class HttpKeys:
    """HTTP-специфичные ключи metadata."""

    LAST_MODIFIED: ClassVar[MetadataKey[str]] = MetadataKey(
        name="transport.http.last_modified",
        decode=str,
        encode=str,
    )
    STATUS: ClassVar[MetadataKey[int]] = MetadataKey(
        name="transport.http.status",
        decode=int,
        encode=str,
    )
