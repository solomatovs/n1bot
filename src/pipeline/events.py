"""События пайплайна — наблюдаемость каждой стадии."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class StageStarted:
    """Стадия пайплайна начала выполнение."""
    stage: str


@dataclass(frozen=True)
class StageCompleted:
    """Стадия пайплайна завершила выполнение."""
    stage: str
    detail: str


@dataclass(frozen=True)
class QueryVariantsGenerated:
    """Сгенерированы переформулировки запроса."""
    variants: List[str]
