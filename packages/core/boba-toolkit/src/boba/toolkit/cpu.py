"""Доля CPU, выделенная запуску: сколько потоков закладывать нативным движкам.

Нативные рантаймы (onnxruntime, BLAS) считают доступные ядра по хосту и не
видят cgroup-квоту запуска: под квотой в одно ядро они всё равно поднимают
пул по числу ядер машины, и потоки дерутся за одну долю. Лаунчер знает квоту
и передаёт её сюда переменной окружения.

Ошибки: не выпускает; неразобранное значение — столько же, сколько ядер у ОС.
"""

from __future__ import annotations

import math
import os
from typing import ClassVar

__all__ = ["CpuBudget"]


class CpuBudget:
    """Число ядер, выделенных процессу; источник — окружение запуска."""

    ENV_VAR: ClassVar[str] = "BOBA_CPU_CORES"
    """Канал доставки квоты от лаунчера; в конфиге такой ручки нет."""

    FALLBACK: ClassVar[int] = 1
    """Запуск без лаунчера и без os.cpu_count(): считаем одно ядро."""

    @classmethod
    def cores(cls) -> int:
        """Сколько потоков закладывать нативному движку; минимум один."""
        raw = os.environ.get(cls.ENV_VAR, "")
        if raw:
            return cls._parse(raw)

        detected = os.cpu_count()
        if detected is None:
            return cls.FALLBACK

        return detected

    @classmethod
    def of_percent(cls, percent: int | None) -> int | None:
        """cgroup-квота в процентах -> ядра, вверх; None — квоты нет."""
        if percent is None:
            return None

        return max(1, math.ceil(percent / 100))

    @classmethod
    def _parse(cls, raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            return cls.FALLBACK

        return max(1, value)
