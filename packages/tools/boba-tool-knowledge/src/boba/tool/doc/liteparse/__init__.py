"""Парсинг документов liteparse внутри песочницы: контракт, вызов, ридер."""

from boba.tool.doc.liteparse.caller import LiteParseCaller
from boba.tool.doc.liteparse.config import SandboxParserConfig
from boba.tool.doc.liteparse.protocol import (
    ParseBytesAnswer,
    ParseBytesRequest,
    ParsedPage,
    ParseParams,
)
from boba.tool.doc.liteparse.reader import SandboxLiteParseReader

__all__ = [
    "LiteParseCaller",
    "ParseBytesAnswer",
    "ParseBytesRequest",
    "ParseParams",
    "ParsedPage",
    "SandboxLiteParseReader",
    "SandboxParserConfig",
]
