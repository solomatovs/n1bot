"""Доменные типы чанкинга — Section, ChunkContentType, ChunkingStrategy Protocol."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterator, List, Protocol, Union

from langchain_core.documents import Document


class ChunkContentType(Enum):
    """Тип контента секции документа при чанкинге."""
    CODE = "code"
    TABLE = "table"
    LIST = "list"
    TEXT = "text"


@dataclass(frozen=True)
class Section:
    """Секция markdown-документа."""
    title: str
    level: int
    content: str


@dataclass(frozen=True)
class SectionProcessed:
    """Секция документа обработана чанкером."""
    section_index: int
    section_total: int
    section_title: str


@dataclass(frozen=True)
class ChunkCreated:
    """Чанк создан чанкером."""
    chunk: Document


ChunkerEvent = Union[SectionProcessed, ChunkCreated]


class ChunkingStrategy(Protocol):
    """Стратегия разбиения документов на чанки (генераторная)."""

    def split_documents(self, docs: List[Document]) -> Iterator[ChunkerEvent]: ...
