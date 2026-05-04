"""Request: общий Protocol для всех request-DTO.

Конкретные Request-DTO живут в transport-пакетах:
- `HttpRequest` в `boba-ext-http-transport` (url + method + headers + auth).
- `FsRequest` в `boba-ext-fs-transport` (path).

Pipeline и `RequestSource[ReqT]` параметризуются конкретным типом —
generic-параметр `ReqT` ограничен этим protocol'ом, что даёт type-safety
(HttpTransport не примет FsRequest).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

__all__ = ["Request"]


@runtime_checkable
class Request(Protocol):
    """Минимальный контракт Request-DTO для прохождения через Pipeline.

    Поля объявлены как read-only properties — frozen-dataclass'ы (HttpRequest,
    FsRequest) удовлетворяют этому без лишних обёрток.
    """

    @property
    def source_id(self) -> str: ...
    """Canonical id итогового документа (например `confluence://host/page/12345`).

    RequestSource ставит сюда canonical, который попадёт в `Section.source_id`
    каждого чанка. Если RequestSource не знает — использует transport-уровень
    (URL для HTTP, `fs:/path` для FS)."""

    @property
    def metadata(self) -> Mapping[str, str]: ...
    """Hint'ы для обогащения Chunk.metadata. Каждый слой добавляет свои
    ключи (merge), не теряя предыдущие."""
