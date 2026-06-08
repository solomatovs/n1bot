"""boba-liteparse — единый toolchain поверх liteparse.

Экспорт: LiteParseEngine (parse / parse_native / search_items),
LiteParseParams (настройки парсера), LiteParseError (единая ошибка),
а также re-export ParseResult/ParseError из liteparse — чтобы
потребители не импортировали upstream-liteparse напрямую.
"""

from __future__ import annotations

from boba.liteparse.engine import LiteParseEngine
from boba.liteparse.errors import LiteParseError
from boba.liteparse.params import LiteParseParams
from boba.liteparse.reader import LiteParseReader
from liteparse import ParseError, ParseResult

__all__ = [
    "LiteParseEngine",
    "LiteParseError",
    "LiteParseParams",
    "LiteParseReader",
    "ParseError",
    "ParseResult",
]
