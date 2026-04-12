"""Доменные типы для поиска по документам из папки."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkLocation:
    """Местоположение чанка в исходном файле."""
    source_file: str
    start_line: int
    end_line: int
    start_offset: int
    end_offset: int
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


@dataclass(frozen=True)
class Fragment:
    """Прочитанный фрагмент файла — текст + границы чанка и прочитанного диапазона."""
    text: str
    hit: SearchHit
    read_start_line: int
    read_end_line: int
    read_start_offset: int
    read_end_offset: int

    @property
    def read_label(self) -> str:
        return f"{self.hit.location.source_file}:{self.read_start_line}-{self.read_end_line}"


