"""Контракты для reader'ов разных форматов."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Document:
    """Логическая единица для индексации до чанкования."""

    source_path: str
    text: str
    metadata: dict[str, str] = field(default_factory=dict)


class Reader(Protocol):
    """Протокол reader'а: extension → Iterable[Document]."""

    extensions: tuple[str, ...]

    def read(self, path: str) -> Iterable[Document]: ...
