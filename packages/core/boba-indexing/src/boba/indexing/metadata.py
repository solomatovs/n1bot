"""
Metadata — typed view над Mapping[str, str] для хранения атрибутов документов и чанков.

Каждый слой pipeline (Transport / Reader / Chunker) добавляет свои ключи через
MetadataKey[T] константы:
    - TransportKeys - ключи, проставляемые Transport-слоем:
            etag, mtime, content_type
     - ReaderKeys - ключи, проставляемые Reader-слоем:
            парсинг структуры документа, например doc_type, page_title, heading_path
     - ChunkerKeys - ключи, проставляемые Chunker-слоем:
            нарезка на чанки, например chunk_index, chunk_summary

Типизация — на уровне доступа, к примеру
    md.get(TransportKeys.MTIME) возвращает float | None а не str | None
    избавляя от необходимости ручного decode/encode.

Использование:

    md = Metadata.empty()
    md = md.set(TransportKeys.ETAG, "abc-123")
    md = md.set(TransportKeys.MTIME, 1700000000.0)
    etag: str | None = md.get(TransportKeys.ETAG)        # "abc-123"
    ts: float | None = md.get(TransportKeys.MTIME)       # 1700000000.0

    # Merge при пробросе по слоям (other побеждает при коллизии)
    section_md = raw_doc.metadata.merge(reader_added)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Generic, TypeVar

__all__ = ["ChunkerKeys", "Metadata", "MetadataKey", "ReaderKeys", "TransportKeys"]


T = TypeVar("T")


@dataclass(frozen=True)
class MetadataKey(Generic[T]):
    """Типизированный ключ для Metadata: name + encode/decode.

    name — wire-key (например 'transport.etag').
    Namespace-prefix предотвращает коллизии между слоями pipeline и
    позволяет легко понять источник ключа при отладке
        decode — превращает wire-string в типизированное значение.
        encode — превращает типизированное значение в wire-string.
    """


    name: str
    decode: Callable[[str], T]
    encode: Callable[[T], str]


@dataclass(frozen=True)
class Metadata:
    """Иммутабельный typed view над wire-format Mapping[str, str]."""

    data: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> Metadata:
        """Пустой Metadata."""
        return cls()

    @classmethod
    def from_wire(cls, data: Mapping[str, str]) -> Metadata:
        """Восстановить из wire-формата (JSON / persistent storage)."""
        return cls(data=dict(data))

    def to_wire(self) -> Mapping[str, str]:
        """Wire-формат: копия dict[str, str] для сериализации."""
        return dict(self.data)

    def get(self, key: MetadataKey[T]) -> T | None:
        """Типизированное значение по ключу или None если отсутствует."""
        raw = self.data.get(key.name)
        if raw is None:
            return None
        return key.decode(raw)

    def set(self, key: MetadataKey[T], value: T) -> Metadata:
        """Новый Metadata с установленным ключом+значением (immutable)."""
        return Metadata(data={**self.data, key.name: key.encode(value)})

    def merge(self, other: Metadata) -> Metadata:
        """Слить два Metadata; other побеждает при коллизии ключей."""
        return Metadata(data={**self.data, **other.data})

    def has(self, key: MetadataKey[T]) -> bool:
        """True если ключ присутствует в Metadata."""
        return key.name in self.data


class TransportKeys:
    """Ключи, проставляемые Transport-слоем."""

    ETAG: ClassVar[MetadataKey[str]] = MetadataKey(
        name="transport.etag",
        decode=str,
        encode=str,
    )
    MTIME: ClassVar[MetadataKey[float]] = MetadataKey(
        name="transport.mtime",
        decode=float,
        encode=str,
    )
    CONTENT_TYPE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="transport.content_type",
        decode=str,
        encode=str,
    )


class ReaderKeys:
    """Ключи, проставляемые Reader-слоем (парсинг структуры документа)."""

    DOC_TYPE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.doc_type",
        decode=str,
        encode=str,
    )
    PAGE_TITLE: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.page_title",
        decode=str,
        encode=str,
    )
    HEADING_PATH: ClassVar[MetadataKey[str]] = MetadataKey(
        name="reader.heading_path",
        decode=str,
        encode=str,
    )


class ChunkerKeys:
    """Ключи, проставляемые Chunker-слоем (нарезка на чанки).

    Пусто на старте — добавятся по мере появления конкретных Chunker-impls.
    """
