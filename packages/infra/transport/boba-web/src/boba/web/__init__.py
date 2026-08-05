"""boba.web: web-доступ из песочницы — caller, протокол fetch/grep."""

from boba.web.caller import WebCaller
from boba.web.protocol import (
    WebFetchRequest,
    WebFetchTrailer,
    WebGrepRequest,
    WebGrepRow,
    WebGrepTrailer,
    WebProfile,
)

__all__ = [
    "WebCaller",
    "WebFetchRequest",
    "WebFetchTrailer",
    "WebGrepRequest",
    "WebGrepRow",
    "WebGrepTrailer",
    "WebProfile",
]
