"""Доменные типы для подготовки документов — события и протокол."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol, Union, runtime_checkable


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


@runtime_checkable
class DocumentPreparerService(Protocol):
    """Подготовка документов для индексации — конвертация и копирование."""

    def prepare_folder(
        self, source_dir: Path, output_dir: Path,
    ) -> Iterator[ConvertEvent]: ...
