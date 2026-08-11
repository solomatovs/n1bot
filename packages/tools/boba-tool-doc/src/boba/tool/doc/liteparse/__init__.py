"""Парсинг документов liteparse внутри песочницы: контракт, вызов, ридер."""

from boba.tool.doc.liteparse.protocol import (
    ParseBytesAnswer,
    ParseBytesArgs,
    ParseBytesRequest,
    ParsedPage,
    ParseParams,
    ParseRequest,
)
from boba.tool.doc.liteparse.sandbox import (
    LiteParseCaller,
    SandboxLiteParseReader,
    SandboxParserConfig,
)

__all__ = [
    "LiteParseCaller",
    "ParseBytesAnswer",
    "ParseBytesArgs",
    "ParseBytesRequest",
    "ParseParams",
    "ParseRequest",
    "ParsedPage",
    "SandboxLiteParseReader",
    "SandboxParserConfig",
]
