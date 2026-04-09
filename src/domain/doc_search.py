"""Доменные типы для поиска по документам из папки."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkLocation:
    """Местоположение чанка в исходном файле."""
    source_file: str
    start_line: int
    end_line: int
    section_title: str = ""

    @property
    def label(self) -> str:
        return f"{self.source_file}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class SearchHit:
    """Результат поиска — чанк с его расположением и оценкой."""
    content: str
    location: ChunkLocation
    score: float = 0.0


