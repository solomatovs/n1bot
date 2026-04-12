"""Pipeline — generic конвейер обработки с наблюдаемостью.

PipelineStage — ABC для стадий. Наследники получают проверку pylance.
Pipeline — оркестратор, yield'ит события из стадий по цепочке.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Generic, Iterator, TypeVar

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ABC стадии
# ---------------------------------------------------------------------------

TContext = TypeVar("TContext")
TEvent = TypeVar("TEvent")


class PipelineStage(ABC, Generic[TContext, TEvent]):
    """Одна стадия пайплайна.

    Каждая стадия читает входные данные из ctx, записывает результаты,
    и yield-ит события для наблюдаемости.

    Наследники обязаны реализовать name и run —
    pylance подсветит если забыть.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def run(self, ctx: TContext) -> Iterator[TEvent]: ...


# ---------------------------------------------------------------------------
# Оркестратор
# ---------------------------------------------------------------------------

_TCtx = TypeVar("_TCtx")
_TEvt = TypeVar("_TEvt")


class Pipeline(Generic[_TCtx, _TEvt]):
    """Выполняет последовательность стадий PipelineStage.

    Каждая стадия получает контекст и yield-ит события.
    Оркестратор — генератор, ничего не накапливает.
    """

    def __init__(self, stages: list[PipelineStage[_TCtx, _TEvt]]) -> None:
        self._stages = list(stages)

    @property
    def stage_names(self) -> list[str]:
        return [s.name for s in self._stages]

    def run(self, ctx: _TCtx) -> Iterator[_TEvt]:
        """Выполнить все стадии, yield-я события по мере их появления."""
        for stage in self._stages:
            log.debug("Pipeline: stage '%s'", stage.name)
            yield from stage.run(ctx)
