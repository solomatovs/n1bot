"""boba.web: web-доступ из песочницы — caller, протокол fetch/grep."""

from boba.web.caller import WebCaller
from boba.web.protocol import (
    WebFetchAnswer,
    WebFetchRequest,
    WebGrepAnswer,
    WebGrepRequest,
    WebProfile,
)

__all__ = [
    "WebCaller",
    "WebFetchAnswer",
    "WebFetchRequest",
    "WebGrepAnswer",
    "WebGrepRequest",
    "WebProfile",
]
