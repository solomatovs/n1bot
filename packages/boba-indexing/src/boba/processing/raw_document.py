"""RawDocument: открытый handle от Transport'а с обогащённой metadata.

Контракт жизненного цикла handle: Transport открывает (`with open() as fp`)
**прямо перед `yield`** и закрывает **сразу после возврата control'а в
generator** (после yield). Reader должен прочитать handle ДО следующей
итерации Transport-цикла. На практике это естественно: pipeline.run внутри
`for raw_doc in transport.stream(...): reader.convert(...)` — read
происходит синхронно перед следующим yield.

`source_id` пробрасывается из `Request.source_id` без изменений.
`metadata` — мерж `Request.metadata` + добавления Transport'а
(etag, content_type, mtime). Дальше Reader/Chunker мерж'ат свои поля.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

__all__ = ["BinaryStream", "RawDocument"]


class BinaryStream(Protocol):
    """Минимальный read-only handle: только `read()`. Удовлетворяют:
    `BufferedReader` (от `open(path, 'rb')`), кастомные адаптеры streaming-
    response'ов, in-memory `BytesIO`. Reader использует только этот контракт.
    """

    def read(self, n: int = -1, /) -> bytes: ...


@dataclass(frozen=True)
class RawDocument:
    """Открытый handle + обогащённая metadata. Lifecycle handle — у Transport'а."""

    handle: BinaryStream
    """Уже открытый handle. Reader просто читает (`fp.read()` / `for line in fp`),
    закрытие — обязанность Transport'а через with-block в generator'е."""

    source_id: str
    """Canonical id, проброшен из `Request.source_id`."""

    content_hint: str = ""
    """MIME / extension для логов; не используется для dispatch'а Reader'а."""

    metadata: Mapping[str, str] = field(default_factory=dict)
    """Обогащённая metadata: Request.metadata + transport-specific (etag,
    content_type, mtime). Reader/Chunker мержат свои ключи поверх."""
