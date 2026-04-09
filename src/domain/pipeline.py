"""Pipeline — generic конвейер обработки с наблюдаемостью."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Generic, Iterator, Protocol, TypeVar, runtime_checkable


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# События
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StageStarted:
    """Стадия пайплайна начала выполнение."""
    stage: str


@dataclass(frozen=True)
class StageCompleted:
    """Стадия пайплайна завершила выполнение."""
    stage: str
    detail: str


# ---------------------------------------------------------------------------
# Protocol стадии
# ---------------------------------------------------------------------------

TContext = TypeVar("TContext", contravariant=True)
TEvent = TypeVar("TEvent", covariant=True)


@runtime_checkable
class PipelineStage(Protocol[TContext, TEvent]):
    """Одна стадия пайплайна.

    Каждая стадия читает входные данные из ctx, записывает результаты,
    и yield-ит события для наблюдаемости.
    """

    @property
    def name(self) -> str: ...

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
