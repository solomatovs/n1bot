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
    """Canonical id итогового документа.

    RequestSource обязан установить — Transport не fallback'ит. Обычно это
    стабильный URL по которому документ доступен (для Confluence — viewpage
    URL `https://host/pages/viewpage.action?pageId=12345`, для FS —
    `fs:/abs/path`). НЕ обязан совпадать с URL HTTP-запроса (например, REST
    endpoint с expand-параметрами — отдельно от viewpage URL)."""

    @property
    def metadata(self) -> Mapping[str, str]: ...
    """Hint'ы для обогащения Chunk.metadata. Каждый слой добавляет свои
    ключи (merge), не теряя предыдущие."""
