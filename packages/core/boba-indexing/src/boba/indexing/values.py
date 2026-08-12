"""Значения предметной области: местоположение, коллекция, хеш, ключ, метаданные, план
формата.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Generic, NewType, Protocol, TypeVar, runtime_checkable

__all__ = [
    "BytesContentHash",
    "ChunkLocation",
    "ChunkerKeys",
    "CollectionId",
    "ContentHash",
    "FormatBlock",
    "FormatPlan",
    "IntContentHash",
    "KeyEncoder",
    "Metadata",
    "MetadataKey",
    "ReaderKeys",
    "Sha256TextEncoder",
    "StringContentHash",
    "TransportKeys",
]


@dataclass(frozen=True)
class ChunkLocation:
    """Положение в исходном content: start включительно, end исключительно
    (полуинтервал).
    """

    start: int
    end: int


CollectionId = NewType("CollectionId", str)
"""Идентификатор коллекции в векторной базе; на один backend — много коллекций."""


class ContentHash(ABC):
    """Hash-значение чанка."""

    @abstractmethod
    def to_wire(self) -> str:
        """Wire-format для записи в IndexSink (обычно hex-string)"""
        ...


@dataclass(frozen=True)
class BytesContentHash(ContentHash):
    """Hash как сырые байты digest'а (sha256/blake2b/...)"""

    raw: bytes

    def to_wire(self) -> str:
        return self.raw.hex()


@dataclass(frozen=True)
class IntContentHash(ContentHash):
    """Hash как N-битное целое, сериализуется в hex."""

    value: int
    bits: int = 64

    def to_wire(self) -> str:
        hex_chars = self.bits // 4
        return f"{self.value:0{hex_chars}x}"


@dataclass(frozen=True)
class StringContentHash(ContentHash):
    """Hash уже в строковом виде (hex/base32/base64 — на усмотрение реализации)."""

    text: str

    def to_wire(self) -> str:
        return self.text


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


T = TypeVar("T")


@dataclass(frozen=True)
class MetadataKey(Generic[T]):
    """Типизированный ключ Metadata: wire-name с namespace + encode/decode."""

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
    """Ключи, проставляемые Chunker-слоем; пока пусто."""


@dataclass(frozen=True)
class FormatBlock:
    """Одна семантическая единица body для chunker'а; is_atomic — «не резать char-
    split'ом».
    """

    format_content: str
    raw_content: str
    location: ChunkLocation
    is_atomic: bool = False


@dataclass(frozen=True)
class FormatPlan:
    """План рендера Section в LLM-формат: blocks + repeat_header/footer + block_glue +
    breadcrumb-инфо.
    """

    blocks: tuple[FormatBlock, ...] = ()
    repeat_header: str = ""
    repeat_footer: str = ""
    block_glue: str = "\n\n"
    breadcrumb_level: int | None = None
    breadcrumb_text: str | None = None
