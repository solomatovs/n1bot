"""Логирование IndexEvent-потока в стандартный logger.

Pipeline.run() молча проглатывает поток событий и отдаёт только финальный
IndexStats. LoggedIndexRun.drain() — drop-in замена: потребляет тот же
поток Pipeline.index(...), но пишет каждое событие (per-source
indexed/skipped/failed, cleanup, итоговый run) в переданный logger, и так же
возвращает IndexStats.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import ClassVar

from boba.indexing import (
    CompletedItem,
    IndexEvent,
    IndexStats,
    IndexStatsBuilder,
    RunFinished,
    Severity,
)

__all__ = ["LoggedIndexRun"]


class LoggedIndexRun:
    """Слив IndexEvent-потока с per-event логированием; возвращает IndexStats."""

    _LEVELS: ClassVar[dict[Severity, int]] = {
        Severity.INFO: logging.INFO,
        Severity.WARN: logging.WARNING,
        Severity.ERROR: logging.ERROR,
    }

    @staticmethod
    def drain(
        events: Iterable[IndexEvent],
        logger: logging.Logger,
    ) -> IndexStats:
        """Потребить поток Pipeline.index(...), пишет каждое событие в logger."""
        stats = IndexStatsBuilder().build()
        for event in events:
            LoggedIndexRun._emit(logger, event)
            if isinstance(event, RunFinished):
                stats = event.stats
        return stats

    @staticmethod
    def _emit(logger: logging.Logger, event: IndexEvent) -> None:
        """Одна log-строка на событие: headline() для item'ов, label() для фаз."""
        message = (
            event.headline() if isinstance(event, CompletedItem) else event.label()
        )
        logger.log(LoggedIndexRun._LEVELS[event.severity()], "%s", message)
