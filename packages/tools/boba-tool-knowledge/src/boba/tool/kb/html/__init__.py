"""Разбор HTML внутри песочницы: контракт и вызов payload'а."""

from boba.tool.kb.html.caller import HtmlCaller
from boba.tool.kb.html.protocol import (
    ConfluenceSection,
    ConfluenceSectionsAnswer,
    ConfluenceSectionsRequest,
    HtmlToMarkdownRequest,
    PlainTextRequest,
)

__all__ = [
    "ConfluenceSection",
    "ConfluenceSectionsAnswer",
    "ConfluenceSectionsRequest",
    "HtmlCaller",
    "HtmlToMarkdownRequest",
    "PlainTextRequest",
]
