"""Служебные подкоманды собранной программы boba (Nuitka): их имена и коды
выхода."""

from __future__ import annotations

from enum import IntEnum, StrEnum

__all__ = ["ProgramExit", "ProgramTool"]


class ProgramTool(StrEnum):
    """Служебные подкоманды собранной программы: идут без службы и её
    импортов. launcher и probe зовёт песочница (команды собирает ProgramRoots),
    self-check — сборка после компиляции и проверка образа."""

    LAUNCHER = "launcher"
    PROBE = "probe"
    SELF_CHECK = "self-check"


class ProgramExit(IntEnum):
    """Коды выхода служебных подкоманд."""

    OK = 0
