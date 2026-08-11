"""Работа с вебом: whitelist хостов, чтение страницы и поиск по ней."""

from boba.tool.web._grep import WebGrepConfig
from boba.tool.web.connection import WebConnection
from boba.tool.web.stages import WebStages
from boba.tool.web.tools import WebTools, build_web_tools

__all__ = [
    "WebConnection",
    "WebGrepConfig",
    "WebStages",
    "WebTools",
    "build_web_tools",
]
