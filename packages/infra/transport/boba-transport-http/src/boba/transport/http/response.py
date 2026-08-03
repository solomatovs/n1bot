"""HttpResponse — чистый результат HTTP-запроса, без индексационных типов."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

__all__ = ["ByteStream", "HttpResponse"]


class ByteStream(Protocol):
    """Минимальный read-only поток байт: только read()."""

    def read(self, n: int = -1, /) -> bytes: ...


@dataclass(frozen=True)
class HttpResponse:
    "Статус, заголовки и поток тела; stream живёт только внутри with fetch(...)"

    status: int
    headers: Mapping[str, str]
    stream: ByteStream
