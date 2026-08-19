"""Ридер документов с логом вокруг самого разбора.

Модуль тянет liteparse, поэтому импортируется лениво — в процессе приложения
его быть не должно.

Ошибок своих не выпускает: разбор падает так же, как у LiteParseReader.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from boba.liteparse.engine import LiteParseReader
from boba.text.document import ParsedPage
from boba.toolkit.timing import Elapsed

__all__ = ["LoggingDocumentReader"]

logger = logging.getLogger("boba.tool.kb.confluence.ingest_tools")


class LoggingDocumentReader(LiteParseReader):
    """LiteParseReader, который сообщает о начале и конце разбора.

    Разбор — самая долгая стадия без обращений наружу: OCR и конвертация
    office-форматов считаются минутами. Без этой пары строк зависание тут
    неотличимо от зависшей закачки.
    """

    def parse_pages(self, data: bytes, filename: str) -> Sequence[ParsedPage]:
        logger.info("parse start: %s, %d bytes", filename, len(data))
        elapsed = Elapsed()
        pages = super().parse_pages(data, filename)
        logger.info(
            "parse done: %s -> %d pages in %dms", filename, len(pages), elapsed.ms()
        )

        return pages
