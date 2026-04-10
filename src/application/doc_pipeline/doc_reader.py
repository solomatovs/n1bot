"""Реестр читателей документов — паттерн Strategy.

Единая точка входа для индексации и чтения контекста.
Формат определяется один раз при регистрации стратегии.

    Использование (стадии pipeline):
        from application.doc_pipeline.doc_reader import registry

        for file_path in registry.iter_files(folder):
            for chunk in registry.iter_chunks(file_path):
                ...

        fragment = registry.read_fragment(file_path, hit, expand_lines)

    Добавление нового формата:
        class PdfReader(DocumentReader):
            extensions = frozenset({".pdf"})
            ...
        registry.register(PdfReader())
"""
from __future__ import annotations

import io
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator, List

from langchain_core.documents import Document

from domain.doc_search import Fragment, SearchHit


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

    def read_fragment(self, file_path: Path, hit: SearchHit, expand_lines: int) -> Fragment:
        """Прочитать фрагмент файла с расширением контекста.

        Базовая реализация — общая для всех текстовых форматов.
        Переопределяйте если формат требует специфичной логики.
        """
        return _read_fragment_text(file_path, hit, expand_lines)


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

    # -- Публичный API (фасад для стадий pipeline) --

    def iter_files(self, folder: Path) -> Iterator[Path]:
        """Итерировать поддерживаемые файлы в папке."""
        if not folder.is_dir():
            return
        for f in sorted(folder.iterdir(), key=lambda p: p.name):
            if f.is_file() and self.supports(f) and not f.name.startswith("."):
                yield f

    def count_files(self, folder: Path) -> int:
        """Подсчитать поддерживаемые файлы."""
        if not folder.is_dir():
            return 0
        return sum(
            1 for f in folder.iterdir()
            if f.is_file() and self.supports(f) and not f.name.startswith(".")
        )

    def iter_chunks(self, file_path: Path) -> Iterator[Document]:
        """Разбить файл на чанки — делегирует стратегии."""
        yield from self.get(file_path).iter_chunks(file_path)

    def read_fragment(self, file_path: Path, hit: SearchHit, expand_lines: int) -> Fragment:
        """Прочитать фрагмент — делегирует стратегии."""
        return self.get(file_path).read_fragment(file_path, hit, expand_lines)


# ---------------------------------------------------------------------------
# Общая логика чтения фрагментов (для всех текстовых форматов)
# ---------------------------------------------------------------------------

def _read_fragment_text(file_path: Path, hit: SearchHit, expand_lines: int) -> Fragment:
    """Прочитать фрагмент текстового файла по позициям + расширение на N строк."""
    loc = hit.location

    with open(file_path, encoding="utf-8", errors="replace") as fh:
        read_start_offset, read_start_line = _find_start_expanded(
            fh, loc.start_offset, loc.start_line, expand_lines,
        )

        fh.seek(read_start_offset)
        lines: list[str] = []

        past_end_lines = 0
        while True:
            line = fh.readline()
            if not line:
                break
            lines.append(line.rstrip("\n").rstrip("\r"))
            if fh.tell() >= loc.end_offset:
                past_end_lines += 1
                if past_end_lines > expand_lines:
                    break

        read_end_offset = fh.tell()
        read_end_line = read_start_line + len(lines) - 1

    return Fragment(
        text="\n".join(lines).strip(),
        hit=hit,
        read_start_line=read_start_line,
        read_end_line=read_end_line,
        read_start_offset=read_start_offset,
        read_end_offset=read_end_offset,
    )


def _find_start_expanded(
    fh: io.TextIOWrapper, start_offset: int, start_line: int, expand_lines: int,
) -> tuple[int, int]:
    """Найти позицию на expand_lines строк раньше start_offset."""
    if start_offset == 0 or expand_lines == 0:
        return start_offset, start_line

    fh.seek(0)
    line_positions: list[tuple[int, int]] = [(0, 1)]
    line_num = 1

    while True:
        pos_before = fh.tell()
        if pos_before >= start_offset:
            break
        line = fh.readline()
        if not line:
            break
        line_num += 1
        next_pos = fh.tell()
        if next_pos <= start_offset:
            line_positions.append((next_pos, line_num))

    target_idx = max(0, len(line_positions) - expand_lines)
    return line_positions[target_idx]


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
    from application.doc_pipeline.html_reader import HtmlReader
    from application.doc_pipeline.markdown_reader import MarkdownReader

    reg = DocumentReaderRegistry()
    reg.register(MarkdownReader())
    reg.register(HtmlReader())
    return reg


registry = _create_default_registry()
