"""Реестр читателей документов — паттерн Strategy.

Единая точка входа для индексации и чтения файлов.
Формат определяется один раз при регистрации стратегии.

    Использование:
        from application.readers.registry import registry

        for file_path in registry.iter_files(folder):
            for chunk in registry.iter_chunks(file_path):
                ...

    Добавление нового формата:
        class PdfReader(DocumentReader):
            extensions = frozenset({".pdf"})
            ...
        registry.register(PdfReader())
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

from langchain_core.documents import Document


# ---------------------------------------------------------------------------
# Протокол стратегии
# ---------------------------------------------------------------------------

class DocumentReader(ABC):
    """Стратегия чтения документов определённого формата.

    Подклассы реализуют:
        extensions  — множество расширений (.md, .html, ...)
        iter_chunks — разбиение файла на чанки для индексации
    """

    @property
    @abstractmethod
    def extensions(self) -> frozenset[str]:
        """Поддерживаемые расширения (lowercase, с точкой)."""

    @abstractmethod
    def iter_chunks(self, file_path: Path) -> Iterator[Document]:
        """Разбить файл на чанки. Каждый Document содержит метаданные с позициями."""


# ---------------------------------------------------------------------------
# Реестр стратегий
# ---------------------------------------------------------------------------

class DocumentReaderRegistry:
    """Реестр: расширение файла → стратегия чтения."""

    def __init__(self) -> None:
        self._readers: dict[str, DocumentReader] = {}

    def register(self, reader: DocumentReader) -> None:
        """Зарегистрировать стратегию для её расширений."""
        for ext in reader.extensions:
            self._readers[ext.lower()] = reader

    def get(self, file_path: Path) -> DocumentReader:
        """Получить стратегию по расширению файла. KeyError если формат не поддержан."""
        return self._readers[file_path.suffix.lower()]

    def supports(self, file_path: Path) -> bool:
        return file_path.suffix.lower() in self._readers

    @property
    def supported_extensions(self) -> frozenset[str]:
        return frozenset(self._readers.keys())

    def iter_files(self, folder: Path) -> Iterator[Path]:
        """Итерировать поддерживаемые файлы в папке."""
        if not folder.is_dir():
            return
        for f in sorted(folder.iterdir(), key=lambda p: p.name):
            if f.is_file() and self.supports(f) and not f.name.startswith("."):
                yield f

    def iter_chunks(self, file_path: Path) -> Iterator[Document]:
        """Разбить файл на чанки — делегирует стратегии."""
        yield from self.get(file_path).iter_chunks(file_path)


# ---------------------------------------------------------------------------
# Утилита для build_chunk (общая для стратегий)
# ---------------------------------------------------------------------------

def build_chunk(
    filename: str,
    section_title: str,
    content: str,
    start_line: int,
    end_line: int,
    start_offset: int,
    end_offset: int,
) -> Document | None:
    """Собрать Document из контента секции. None если пустой."""
    if not content.strip():
        return None
    return Document(
        page_content=content.strip(),
        metadata={
            "source_file": filename,
            "start_line": start_line,
            "end_line": end_line,
            "start_offset": start_offset,
            "end_offset": end_offset,
            "section_title": section_title,
        },
    )


# ---------------------------------------------------------------------------
# Глобальный реестр — конфигурируется при импорте
# ---------------------------------------------------------------------------

def _create_default_registry() -> DocumentReaderRegistry:
    """Создать реестр с зарегистрированными стратегиями."""
    from application.readers.html import HtmlReader
    from application.readers.markdown import MarkdownReader

    reg = DocumentReaderRegistry()
    reg.register(MarkdownReader())
    reg.register(HtmlReader())
    return reg


registry = _create_default_registry()
