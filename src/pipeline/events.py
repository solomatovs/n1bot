"""Базовые события пайплайна — общие для всех пайплайнов."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageStarted:
    """Стадия пайплайна начала выполнение."""
    stage: str


@dataclass(frozen=True)
class StageCompleted:
    """Стадия пайплайна завершила выполнение."""
    stage: str
    detail: str
