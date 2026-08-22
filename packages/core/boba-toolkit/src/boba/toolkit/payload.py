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
    и передаёт аргументом зиготы. Тела инструментов, форкнутые из зиготы,
    берут запомненный уровень, а не собственный источник.
    """

    LEVEL_ENV: ClassVar[str] = "BOBA_LOG_LEVEL"
    """Уровень для запуска процесса руками, без зиготы."""

    FALLBACK_LEVEL: ClassVar[str] = "INFO"
    """Ни аргумента, ни переменной: процесс запущен вне приложения."""

    _adopted: ClassVar[int] = 0
    """Уровень, принятый от хоста; 0 — аргумента не было."""

    @classmethod
    def setup(cls, level: str) -> None:
        """Ставится один раз на процесс; уровень приходит от хоста."""
        cls._adopted = cls._parse(level)

        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(PayloadLogFormatter())
        logging.basicConfig(level=cls._adopted, handlers=[handler], force=True)

    @classmethod
    def level(cls) -> int:
        """Уровень тела инструмента: от зиготы, иначе из окружения."""
        if cls._adopted:
            return cls._adopted

        raw = os.environ.get(cls.LEVEL_ENV, cls.FALLBACK_LEVEL)
        return cls._parse(raw)

    @staticmethod
    def _parse(raw: str) -> int:
        resolved = logging.getLevelName(raw.upper())
        if isinstance(resolved, int):
            return resolved

        return logging.INFO
