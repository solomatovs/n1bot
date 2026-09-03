"""Метрики вкладки через CDP для браузерных замеров ленты: heap после принудительного
GC, узлы DOM, накопленное время скриптов и раскладки.

Ошибки:
PerfError — CDP не отдал метрику, на которую опирается замер.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar

from playwright.sync_api import Page
from pydantic import BaseModel, ConfigDict

__all__ = ["PageMeter", "PageMetrics", "PerfError", "TurnSample", "TurnSeries"]


class PerfError(RuntimeError):
    """Метрика не получена."""


class CdpMetric(StrEnum):
    """Имена метрик Performance.getMetrics, которыми пользуется замер."""

    HEAP_USED = "JSHeapUsedSize"
    NODES = "Nodes"
    LISTENERS = "JSEventListeners"
    SCRIPT = "ScriptDuration"
    LAYOUT = "LayoutDuration"
    TASK = "TaskDuration"
    LAYOUTS = "LayoutCount"


class PageMetrics(BaseModel):
    """Снимок метрик вкладки; длительности накопительные с открытия страницы."""

    model_config = ConfigDict(frozen=True)

    heap_mb: float
    nodes: int
    listeners: int
    script_s: float
    layout_s: float
    task_s: float
    layouts: int

    @classmethod
    def of(cls, raw: Sequence[Mapping[str, Any]]) -> PageMetrics:
        values: dict[str, float] = {}
        for item in raw:
            values[str(item["name"])] = float(item["value"])

        missing: list[str] = []
        for metric in CdpMetric:
            if metric.value not in values:
                missing.append(metric.value)

        if missing:
            raise PerfError(f"cdp metrics are missing: {missing}")

        return cls(
            heap_mb=values[CdpMetric.HEAP_USED] / 1e6,
            nodes=int(values[CdpMetric.NODES]),
            listeners=int(values[CdpMetric.LISTENERS]),
            script_s=values[CdpMetric.SCRIPT],
            layout_s=values[CdpMetric.LAYOUT],
            task_s=values[CdpMetric.TASK],
            layouts=int(values[CdpMetric.LAYOUTS]),
        )


class TurnSample(BaseModel):
    """Один ход: сколько кадров пришло и что он стоил вкладке."""

    model_config = ConfigDict(frozen=True)

    turn: int
    wall_s: float
    frames: int
    heap_mb: float
    nodes: int
    script_ms: float
    layout_ms: float
    task_ms: float
    layouts: int

    @classmethod
    def between(
        cls,
        turn: int,
        wall_s: float,
        frames: int,
        before: PageMetrics,
        after: PageMetrics,
    ) -> TurnSample:
        return cls(
            turn=turn,
            wall_s=wall_s,
            frames=frames,
            heap_mb=after.heap_mb,
            nodes=after.nodes,
            script_ms=(after.script_s - before.script_s) * 1000,
            layout_ms=(after.layout_s - before.layout_s) * 1000,
            task_ms=(after.task_s - before.task_s) * 1000,
            layouts=after.layouts - before.layouts,
        )

    @property
    def script_ms_per_frame(self) -> float:
        if not self.frames:
            return 0.0

        return self.script_ms / self.frames


class TurnSeries:
    """Ряд ходов одного треда: стоимость хода и рост heap с длиной ленты."""

    def __init__(self, samples: Sequence[TurnSample]) -> None:
        if not samples:
            raise PerfError("series needs at least one turn")

        self._samples = list(samples)

    @property
    def samples(self) -> Sequence[TurnSample]:
        return self._samples

    def max_frames(self) -> int:
        return max(sample.frames for sample in self._samples)

    def max_script_ms_per_frame(self) -> float:
        return max(sample.script_ms_per_frame for sample in self._samples)

    def mean_script_ms(self) -> float:
        return self._mean_script(self._samples)

    MIN_HEAP_SAMPLES: ClassVar[int] = 2
    """Рост кучи считается между первым и последним ходом: нужны хотя бы два."""

    def heap_kb_per_turn(self) -> float:
        if len(self._samples) < self.MIN_HEAP_SAMPLES:
            raise PerfError("heap growth needs at least two turns")

        first = self._samples[0]
        last = self._samples[-1]
        return (last.heap_mb - first.heap_mb) * 1000 / (len(self._samples) - 1)

    @staticmethod
    def _mean_script(samples: Sequence[TurnSample]) -> float:
        total = 0.0
        for sample in samples:
            total += sample.script_ms

        return total / len(samples)

    def describe(self) -> str:
        lines: list[str] = []
        for sample in self._samples:
            lines.append(
                f"turn {sample.turn:>3}: {sample.frames:>4} frames, "
                f"js {sample.script_ms:6.0f} ms, layout {sample.layout_ms:5.0f} ms, "
                f"heap {sample.heap_mb:5.1f} MB, nodes {sample.nodes}"
            )

        return "\n".join(lines)


class PageMeter:
    """CDP-сессия вкладки: снимок метрик после принудительного GC, чтобы heap
    отражал удерживаемое, а не мусор между сборками.
    """

    GC_SETTLE_MS: ClassVar[int] = 200

    def __init__(self, page: Page) -> None:
        self._page = page
        self._cdp = page.context.new_cdp_session(page)
        self._cdp.send("Performance.enable")
        self._cdp.send("HeapProfiler.enable")

    def snapshot(self) -> PageMetrics:
        self._cdp.send("HeapProfiler.collectGarbage")
        self._page.wait_for_timeout(self.GC_SETTLE_MS)
        answer = self._cdp.send("Performance.getMetrics")
        return PageMetrics.of(answer["metrics"])
