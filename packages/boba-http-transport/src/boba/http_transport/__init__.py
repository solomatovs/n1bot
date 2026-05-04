"""boba-ext-http-transport: generic HTTP transport + auth callables."""

from __future__ import annotations

from boba.http_transport.auth import BasicAuth, PatAuth
from boba.http_transport.request import HttpRequest
from boba.http_transport.transport import HttpTransport

__all__ = [
    "BasicAuth",
    "HttpRequest",
    "HttpTransport",
    "PatAuth",
]
