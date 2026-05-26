"""Включение debug-логирования HTTP-трафика и SQL по флагам Settings."""

import http.client
import logging
from typing import ClassVar

from langgraph_rag.config import Settings


class DebugLogging:
    """Активирует http.client debug и sqlalchemy.engine logger согласно Settings."""

    _SQL_LEVELS: ClassVar[dict[str, int]] = {
        "off": logging.WARNING,
        "info": logging.INFO,
        "debug": logging.DEBUG,
    }

    @classmethod
    def apply(cls, settings: Settings) -> None:
        """Включает выбранные debug-каналы; вызывать после logging.basicConfig."""
        if settings.debug_http:
            http.client.HTTPConnection.debuglevel = 1
            logging.getLogger("urllib3").setLevel(logging.DEBUG)
            logging.getLogger("requests").setLevel(logging.DEBUG)

        level = cls._SQL_LEVELS[settings.debug_sql]
        logging.getLogger("sqlalchemy.engine").setLevel(level)
