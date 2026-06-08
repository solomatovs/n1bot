"""FsKeys — typed MetadataKey'и, проставляемые FS-стадиями.

Дополняет TransportKeys (MTIME/CONTENT_TYPE/ETAG) FS-специфичными
полями: размер файла, абсолютный путь, suffix-расширение.
"""

from __future__ import annotations

from typing import ClassVar

from boba.indexing import MetadataKey

__all__ = ["FsKeys"]


class FsKeys:
    """FS-специфичные ключи metadata."""

    SIZE: ClassVar[MetadataKey[int]] = MetadataKey(
        name="transport.fs.size",
        decode=int,
        encode=str,
    )
    PATH: ClassVar[MetadataKey[str]] = MetadataKey(
        name="transport.fs.path",
        decode=str,
        encode=str,
    )
    NAME: ClassVar[MetadataKey[str]] = MetadataKey(
        name="transport.fs.name",
        decode=str,
        encode=str,
    )
    SUFFIX: ClassVar[MetadataKey[str]] = MetadataKey(
        name="transport.fs.suffix",
        decode=str,
        encode=str,
    )
