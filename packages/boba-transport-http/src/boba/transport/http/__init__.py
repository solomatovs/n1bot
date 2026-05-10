"""boba.transport.http — HTTP transport для индексации.

Стадии:
- `HttpRequest(url, source_id, method, headers, auth, metadata)` — DTO
  одного HTTP-запроса. `source_id` обязательный (caller-supplied
  canonical id).
- `HttpTransport(timeout_sec, verify)` — выполняет запросы через
  `httpx.Client`, отдаёт `RawDocument` со streaming-handle.
- `AuthApplier = Callable[[dict[str, Any]], None]` — мутирует kwargs
  `httpx.Client` перед его созданием. Готовые: `PatAuth(token)` для
  Atlassian-PAT, `BasicAuth(user, password)` для basic-auth.

Использование:
    auth = PatAuth(token=os.environ["CONFLUENCE_PAT"])
    requests = [
        HttpRequest(url="https://...", source_id=SourceId("..."), auth=auth)
    ]
    transport = HttpTransport(timeout_sec=60.0)
    for raw in transport.stream(ctx, requests):
        body = raw.handle.read()
        ...
"""

from __future__ import annotations

from boba.transport.http.auth import AuthApplier, BasicAuth, PatAuth
from boba.transport.http.keys import HttpKeys
from boba.transport.http.request import HttpRequest
from boba.transport.http.transport import HttpTransport

__all__ = [
    "AuthApplier",
    "BasicAuth",
    "HttpKeys",
    "HttpRequest",
    "HttpTransport",
    "PatAuth",
]
