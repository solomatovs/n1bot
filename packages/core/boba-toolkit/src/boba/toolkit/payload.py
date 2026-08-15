"""Логи процесса в песочнице: кадры `sandbox-log:` в stderr.

Свободный текст stderr на исход запуска не влияет — его читает релей хоста
и сливает в общий журнал приложения. Процессу достаточно обычного
logging.getLogger.

Ошибки: не выпускает.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import ClassVar

from boba.toolkit.launcher import LaunchPayload

__all__ = [
    "PayloadLogging",
]


class PayloadLogFormatter(logging.Formatter):
    """Запись логера -> кадр `sandbox-log:` одной строкой."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return LaunchPayload.encode_log(record.levelname, record.name, message)


class PayloadLogging:
    """Логер процесса песочницы: кадры в stderr вместо свободного текста.

    Уровень не настраивается здесь: его вычисляет хост из своей секции logger
    и передаёт переменной окружения — так у настройки остаётся один источник.
    """

    LEVEL_ENV: ClassVar[str] = "BOBA_LOG_LEVEL"
    """Канал доставки уровня от хоста; в конфиге такой ручки нет."""

    FALLBACK_LEVEL: ClassVar[str] = "INFO"
    """Только для запуска процесса руками, без хоста."""

    @classmethod
    def setup(cls) -> None:
        """Ставится один раз на процесс; уровень приходит от хоста."""
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(PayloadLogFormatter())
        logging.basicConfig(level=cls.level(), handlers=[handler], force=True)

    @classmethod
    def level(cls) -> int:
        raw = os.environ.get(cls.LEVEL_ENV, cls.FALLBACK_LEVEL).upper()
        resolved = logging.getLevelName(raw)
        if isinstance(resolved, int):
            return resolved
        return logging.INFO
