"""boba.transport.http — чистый HTTP-транспорт.

Стадии:
- HttpProfile(base_url, auth, timeout, retry, ssl) — DTO соединения (всё кроме
  url/method/headers/params). На нём конструируется транспорт.
- HttpRequest(url, method, headers, params, content/data/files/json) — план
  одного запроса; headers/params и тело задаются целиком здесь (профиль их не
  несёт). Body-поля прозрачно уходят в httpx build_request; files/стриминговый
  content позволяют слать файл с диска без загрузки в память.
- HttpTransport(profile) — исполняет запрос через переиспользуемый
  httpx.Client, отдаёт HttpResponse со streaming-телом. Индексацию
  (RawDocument/metadata/source_id) не знает — это дело потребителя.
- HttpResponse(status, headers, stream) — чистый результат запроса.
- auth: WebAuth — discriminated union auth-методов (none/basic/bearer/
  digest), задаётся в профиле; httpx_auth() идёт в httpx.Client(auth=...).

Использование:
    profile = HttpProfile(auth=BasicAuth(method="basic", user="u", password="p"))
    with HttpTransport(profile) as transport:
        with transport.fetch(HttpRequest(url="https://...")) as resp:
            body = resp.stream.read()
"""

from __future__ import annotations

from boba.transport.http.auth import (
    BasicAuth,
    BearerAuth,
    DigestAuth,
    HttpxBearerAuth,
    NoneAuth,
    WebAuth,
)
from boba.transport.http.connection import HttpProfile
from boba.transport.http.request import HttpRequest
from boba.transport.http.response import ByteStream, HttpResponse
from boba.transport.http.transport import HttpTransport

__all__ = [
    "BasicAuth",
    "BearerAuth",
    "ByteStream",
    "DigestAuth",
    "HttpProfile",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "HttpxBearerAuth",
    "NoneAuth",
    "WebAuth",
]
