"""Работа с вебом: whitelist хостов, чтение страницы и поиск по ней."""

from boba.chainlit2.agent.tools.web._grep import WebGrepConfig
from boba.chainlit2.agent.tools.web.connection import WebConnection
from boba.chainlit2.agent.tools.web.tools import WebTools, build_web_tools

__all__ = ["WebConnection", "WebGrepConfig", "WebTools", "build_web_tools"]
