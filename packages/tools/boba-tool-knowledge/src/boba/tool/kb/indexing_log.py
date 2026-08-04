"""Логирование IndexEvent-потока в logger; drop-in замена Pipeline.run()."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable
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
    async def drain(
        events: AsyncIterable[IndexEvent],
        logger: logging.Logger,
    ) -> IndexStats:
        """Потребить поток Pipeline.index(...), пишет каждое событие в logger."""
        stats = IndexStatsBuilder().build()
        async for event in events:
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
