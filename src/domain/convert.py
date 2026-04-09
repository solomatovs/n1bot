"""Доменные типы для конвертации файлов — события."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class ConvertFileStarted:
    """Начата конвертация файла."""
    filename: str
    index: int
    total: int


@dataclass(frozen=True)
class ConvertFileDone:
    """Файл успешно сконвертирован."""
    source: str
    target: str
    index: int
    total: int


@dataclass(frozen=True)
class ConvertFileFailed:
    """Не удалось сконвертировать файл."""
    filename: str
    error: str
    index: int
    total: int


@dataclass(frozen=True)
class ConvertDone:
    """Конвертация завершена."""
    ok_count: int
    failed_count: int


ConvertEvent = Union[ConvertFileStarted, ConvertFileDone, ConvertFileFailed, ConvertDone]
