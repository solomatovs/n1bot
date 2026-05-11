"""boba.transport.http — HTTP transport для индексации.

Стадии:
- `HttpRequest(url, source_id, method, headers, auth, metadata)` — DTO
  одного HTTP-запроса. `source_id` обязательный (caller-supplied
  canonical id).
- `HttpTransport(timeout_sec, verify)` — выполняет запросы через
  `httpx.Client`, отдаёт `RawDocument` со streaming-handle.
- `auth: httpx.Auth | None` — httpx-native auth, пробрасывается в
  `httpx.Client(auth=...)`. Для Basic — `httpx.BasicAuth(username, password)`.
  Любая кастомная схема — `httpx.Auth`-наследник; формат-специфичные
  реализации (Bearer/PAT/OAuth/...) живут в соответствующих feature-пакетах.

Использование:
    auth = httpx.BasicAuth("user", "password")
    requests = [
        HttpRequest(url="https://...", source_id=SourceId("..."), auth=auth)
    ]
    transport = HttpTransport(timeout_sec=60.0)
    for raw in transport.stream(ctx, requests):
        body = raw.handle.read()
        ...
"""

from __future__ import annotations

from boba.transport.http.keys import HttpKeys
from boba.transport.http.request import HttpRequest
from boba.transport.http.transport import HttpTransport

__all__ = [
    "HttpKeys",
    "HttpRequest",
    "HttpTransport",
]
