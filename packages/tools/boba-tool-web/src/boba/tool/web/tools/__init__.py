"""Web tools: web_fetch + web_download + web_grep."""

from __future__ import annotations

from boba.tool.web.tools.download import WebDownloadConfig, web_download
from boba.tool.web.tools.fetch import WebFetchConfig, web_fetch
from boba.tool.web.tools.grep import WebGrepConfig, web_grep

__all__ = [
    "WebDownloadConfig",
    "WebFetchConfig",
    "WebGrepConfig",
    "web_download",
    "web_fetch",
    "web_grep",
]
