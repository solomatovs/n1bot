"""Разбор HTML внутри песочницы: контракт, вызов узла и узлы реестра."""

from boba.tool.kb.html.caller import HtmlCaller
from boba.tool.kb.html.protocol import (
    ConfluenceSection,
    ConfluenceSectionsAnswer,
    ConfluenceSectionsRequest,
    HtmlNode,
    HtmlToMarkdownRequest,
    PlainTextRequest,
)
from boba.tool.kb.html.stages import HtmlStages

__all__ = [
    "ConfluenceSection",
    "ConfluenceSectionsAnswer",
    "ConfluenceSectionsRequest",
    "HtmlCaller",
    "HtmlNode",
    "HtmlStages",
    "HtmlToMarkdownRequest",
    "PlainTextRequest",
]
