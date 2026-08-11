"""boba.web: web-доступ из песочницы — caller, контракт узлов fetch/grep."""

from boba.web.caller import WebCaller
from boba.web.protocol import (
    WebFetchArgs,
    WebFetchRequest,
    WebFetchTrailer,
    WebGrepArgs,
    WebGrepRequest,
    WebGrepRow,
    WebGrepTrailer,
    WebNodes,
    WebOp,
    WebProfile,
)

__all__ = [
    "WebCaller",
    "WebFetchArgs",
    "WebFetchRequest",
    "WebFetchTrailer",
    "WebGrepArgs",
    "WebGrepRequest",
    "WebGrepRow",
    "WebGrepTrailer",
    "WebNodes",
    "WebOp",
    "WebProfile",
]
