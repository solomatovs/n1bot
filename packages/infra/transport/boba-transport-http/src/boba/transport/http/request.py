"HttpRequest — чистый план одного HTTP-запроса: url + method + headers/params + body"

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["HttpRequest"]


@dataclass(frozen=True)
class HttpRequest:
    """План одного HTTP-запроса; body-поля уходят в httpx build_request как есть.

    Retry реплеит in-memory body и seekable-files; генератор content одноразов.
    """

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    content: bytes | str | Iterable[bytes] | None = None
    data: Mapping[str, Any] | None = None
    files: Any | None = None
    json: Any | None = None
