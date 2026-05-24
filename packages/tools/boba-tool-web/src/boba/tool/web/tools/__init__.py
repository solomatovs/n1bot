"""Web tools: web_fetch + web_download."""

from __future__ import annotations

from boba.tool.web.tools.download import WebDownloadConfig, web_download
from boba.tool.web.tools.fetch import WebFetchConfig, web_fetch

__all__ = [
    "WebDownloadConfig",
    "WebFetchConfig",
    "web_download",
    "web_fetch",
]
