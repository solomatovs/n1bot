"""KeyEncoder[T] — стабильное хэширование chunk-content в ContentHash для идемпотентной индексации."""

from __future__ import annotations

import hashlib
from typing import ClassVar, Protocol, TypeVar, runtime_checkable

from boba.indexing.content_hash import BytesContentHash, ContentHash

__all__ = [
    "KeyEncoder",
    "Sha256TextEncoder",
]

T_contra = TypeVar("T_contra", contravariant=True)


@runtime_checkable
class KeyEncoder(Protocol[T_contra]):
    """T -> ContentHash: стабильный hash chunk-content для idempotent re-index."""

    def encode(self, content: T_contra) -> ContentHash:
        """Вернуть стабильный (одинаковый для одинакового контента) ContentHash."""
        ...


class Sha256TextEncoder(KeyEncoder[str]):
    """SHA-256 поверх str: encode UTF-8 -> 32-байтовый BytesContentHash."""

    _ENCODING: ClassVar[str] = "utf-8"

    def encode(self, content: str) -> ContentHash:
        digest = hashlib.sha256(content.encode(self._ENCODING)).digest()
        return BytesContentHash(raw=digest)
