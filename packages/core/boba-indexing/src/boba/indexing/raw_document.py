"""
RawDocument - открытый handle (файловый дескриптор) + metadata от Transport

жизненный цикл RawDocument.handle контролируется Transport слоем а не у Reader
потому что Transport открывает handle и сам же гарантируем его закрытие через with

source_id - пробрасывается из Request.source_id без изменений
metadata — пробрасывается с предыдущего уровня и обогощяется данными от Transport
    например, etag для HTTP, mtime для FS
    Reader/Chunker мержат свои ключи поверх
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from boba.indexing.metadata import Metadata
from boba.indexing.sections import SourceId

__all__ = ["BinaryStream", "RawDocument"]


class BinaryStream(Protocol):
    """
    Минимальный read-only handle: только read()
    Полность совместим с io.BufferedIOBase (метод read()), а значит может быть:
        - BufferedReader (от open(path, 'rb'))
        - кастомных адаптеров streaming-response
        - in-memory BytesIO
        - простым файловым дескриптором от os.open(path, O_RDONLY)
    Абсолюно все, что имеет метод read()
    который возвращает bytes и не требует дополнительного контекста
    (например, позиционирования курсора)

    Позволяет Reader'у не зависеть от конкретного способа получения данных: HTTP, FS, S3
    """

    def read(self, n: int = -1, /) -> bytes: ...


@dataclass(frozen=True)
class RawDocument:
    """
    Открытый handle + metadata.
    Lifecycle handle — у Transport
    """

    handle: BinaryStream
    """
    Уже открытый handle.
    Reader просто читает (fp.read() / for line in fp),
    закрытие — обязанность Transport'а через with-block в generator'е
    """

    source_id: SourceId
    """Canonical id, проброшен из Request.source_id."""

    metadata: Metadata = field(default_factory=Metadata.empty)
    """
    metadata = Request.metadata + transport-specific keys:
        TransportKeys.ETAG, TransportKeys.MTIME, TransportKeys.CONTENT_TYPE
        и т.п.

    Reader/Chunker могут мержить свои ключи поверх, не теряя предыдущие.
    """
