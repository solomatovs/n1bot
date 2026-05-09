"""
KeyEncoder[T] — алгоритм хэширования chunk-content в `ContentHash`.

Необходимо для идемпотентной индексации: при повторной индексации без изменений
контента мы должны получать тот же hash, чтобы `IndexSink.reconcile` мог
пропустить уже проиндексированные чанки.

Возвращает `ContentHash` (а не сырую строку) — concrete impl выбирает internal
storage (bytes / int / str) сам через subclass'ы `ContentHash`. Caller получает
строковую форму через `ContentHash.to_wire()` для записи в IndexSink.

Конкретный алгоритм (SHA-256, xxhash, blake2b, любой кастом) выбирает
пользовательский код, реализующий `KeyEncoder[T].encode(content: T) → ContentHash`.
"""

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
    """T → ContentHash: стабильный hash chunk-content для idempotent re-index."""

    def encode(self, content: T_contra) -> ContentHash:
        """
        Вернуть стабильный ContentHash для content'а.

        Стабильный означает одинаковый для одинакового контента — это
        обеспечивает идемпотентность индексации.
        Конкретный impl выбирает алгоритм (SHA-256 / xxhash / blake2b / ...)
        и формат ContentHash-subclass'а (BytesContentHash / IntContentHash /
        StringContentHash) на своё усмотрение.
        """
        ...


class Sha256TextEncoder(KeyEncoder[str]):
    """SHA-256 поверх str: encode UTF-8 → 32-байтовый BytesContentHash."""

    _ENCODING: ClassVar[str] = "utf-8"

    def encode(self, content: str) -> ContentHash:
        digest = hashlib.sha256(content.encode(self._ENCODING)).digest()
        return BytesContentHash(raw=digest)
