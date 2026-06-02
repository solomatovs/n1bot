"""Логирование IndexEvent-потока индексатора в стандартный logger.

`Indexer.invoke()` молча проглатывает поток событий и отдаёт только финальный
`IndexStats`. `LoggedIndexRun.invoke()` — drop-in замена: прогоняет тот же
`Indexer.stream()`, но пишет каждое событие (per-source indexed/skipped/failed,
cleanup, итоговый run) в переданный logger, и так же возвращает `IndexStats`.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from boba.indexing import (
    CompletedItem,
    Indexer,
    IndexerConfig,
    IndexEvent,
    IndexStats,
    IndexStatsBuilder,
    PipelineContext,
    RunFinished,
    Severity,
)

__all__ = ["LoggedIndexRun"]


class LoggedIndexRun:
    """Прогон `Indexer.stream()` с per-event логированием; возвращает `IndexStats`."""

    _LEVELS: ClassVar[dict[Severity, int]] = {
        Severity.INFO: logging.INFO,
        Severity.WARN: logging.WARNING,
        Severity.ERROR: logging.ERROR,
    }

    @staticmethod
    def invoke(
        indexer: Indexer[Any, Any],
        ctx: PipelineContext,
        config: IndexerConfig[Any],
        logger: logging.Logger,
    ) -> IndexStats:
        """Аналог `indexer.invoke(ctx, config)`, но каждое событие пишется в logger."""
        stats = IndexStatsBuilder().build()
        for event in indexer.stream(ctx, config):
            LoggedIndexRun._emit(logger, event)
            if isinstance(event, RunFinished):
                stats = event.stats
        return stats

    @staticmethod
    def _emit(logger: logging.Logger, event: IndexEvent) -> None:
        """Одна log-строка на событие: `headline()` для item'ов, `label()` для фаз."""
        message = (
            event.headline() if isinstance(event, CompletedItem) else event.label()
        )
        logger.log(LoggedIndexRun._LEVELS[event.severity()], "%s", message)
