"""Настройка логирования."""

from __future__ import annotations

import logging
import sys

_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_NOISY_LOGGERS = (
    "watchdog",
    "httpx",
    "httpcore",
    "engineio.server",
)


def configure_logging(log_level: str = "INFO") -> None:
    level = _LOG_LEVELS.get(log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
