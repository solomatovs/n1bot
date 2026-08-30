"""Возраст процесса по procfs.

Ошибки:
OSError — /proc недоступен (вне linux/procfs).
"""

from __future__ import annotations

import os
from typing import ClassVar

__all__ = ["ProcessAge"]


class ProcessAge:
    """Возраст текущего процесса: сколько прошло от exec до вызова.

    Считается по /proc: starttime процесса (в тиках от загрузки) против
    /proc/uptime. Показывает, сколько заняли fork/exec, обвязка запуска и
    старт интерпретатора с импортами до первой строки кода.
    """

    STAT: ClassVar[str] = "/proc/self/stat"
    UPTIME: ClassVar[str] = "/proc/uptime"

    STARTTIME_INDEX: ClassVar[int] = 19
    """Поле starttime (22-е в stat) после отсечения pid и comm: state — нулевое."""

    @classmethod
    def ms(cls) -> int:
        with open(cls.STAT) as f:
            stat = f.read()

        with open(cls.UPTIME) as f:
            uptime_sec = float(f.read().split()[0])

        # comm в скобках может содержать пробелы: поля берутся после ')'
        fields = stat.rsplit(")", 1)[1].split()
        started_ticks = int(fields[cls.STARTTIME_INDEX])

        ticks_per_sec = os.sysconf("SC_CLK_TCK")
        age_sec = uptime_sec - started_ticks / ticks_per_sec

        return max(int(age_sec * 1000), 0)
