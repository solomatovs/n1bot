"""Ре-экспорт унифицированного `WebAuth` из `boba.transport.http`.

Auth-модели переехали в инфра-слой HTTP (используются и web, и confluence).
Здесь — только ре-экспорт для совместимости импортов внутри web-пакета.
"""

from __future__ import annotations

from boba.transport.http.auth import (
    BasicAuth,
    BearerAuth,
    DigestAuth,
    NoneAuth,
    WebAuth,
)

__all__ = [
    "BasicAuth",
    "BearerAuth",
    "DigestAuth",
    "NoneAuth",
    "WebAuth",
]
