"""Команды запуска внешнего слоя песочницы и пробы cgroup."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class SandboxCommand(StrEnum):
    """Служебные подкоманды собранной программы: идут без службы и её
    импортов. launcher и probe зовёт песочница (команды собирает ProgramRoots),
    """

    LAUNCHER = "launcher"
    PROBE = "probe"


class ProgramExit(IntEnum):
    """Коды выхода служебных подкоманд."""

    OK = 0
