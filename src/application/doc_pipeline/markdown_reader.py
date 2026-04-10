"""Единый модуль чтения markdown-документов — индексация и извлечение контекста.

Оба процесса работают с одной структурой: markdown-файл разбитый на секции
по заголовкам (# / ## / ###). Каждая секция — потенциальный чанк с позицией.

Индексация:
    iter_chunks(file_path) → Document с метаданными (file, line, offset, section)

Чтение контекста:
    read_fragment(file_path, start_offset, end_offset, expand_lines) → текст

Файловые операции:
    iter_files(folder) → Iterator[Path]
    count_files(folder) → int
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Iterator, List

from langchain_core.documents import Document

from domain.doc_search import Fragment, SearchHit

_SUPPORTED_EXTENSIONS = {".md", ".txt"}
_HEADING_PATTERN = re.compile(r"^(#+)\s+(.+)$")


# ---------------------------------------------------------------------------
# Файловые операции
# ---------------------------------------------------------------------------

def count_files(folder: Path) -> int:
    """Подсчитать количество поддерживаемых файлов."""
    if not folder.is_dir():
        return 0
    return sum(1 for f in folder.iterdir() if _is_supported(f))


def iter_files(folder: Path) -> Iterator[Path]:
    """Лениво итерировать поддерживаемые файлы."""
    if not folder.is_dir():
        return
    for f in sorted(folder.iterdir(), key=lambda p: p.name):
        if _is_supported(f):
            yield f


def _is_supported(f: Path) -> bool:
    return f.is_file() and f.suffix.lower() in _SUPPORTED_EXTENSIONS and not f.name.startswith(".")


# ---------------------------------------------------------------------------
# Индексация — разбиение файла на чанки по секциям
# ---------------------------------------------------------------------------

def iter_chunks(file_path: Path) -> Iterator[Document]:
    """Разбить файл на чанки по секциям (заголовкам).

    Читает строка за строкой в текстовом режиме.
    Каждый чанк содержит метаданные: source_file, start/end_line, start/end_offset, section_title.
    Позиции (offset) — от tell(), корректны для seek() с многобайтовыми символами.
    """
    section_title = ""
    section_lines: List[str] = []
    section_start_line = 1
    section_start_offset = 0
    line_number = 0

    with open(file_path, encoding="utf-8", errors="replace") as fh:
        while True:
            line_offset = fh.tell()
            line = fh.readline()
            if not line:
                break
            line_number += 1
            line = line.rstrip("\n").rstrip("\r")
            heading_match = _HEADING_PATTERN.match(line.strip())

            if heading_match:
                if section_lines:
                    chunk = _build_chunk(
                        file_path.name, section_title, section_lines,
                        section_start_line, line_number - 1,
                        section_start_offset, line_offset,
                    )
                    if chunk is not None:
                        yield chunk
                    section_lines = []
                section_title = heading_match.group(2)
                section_start_line = line_number
                section_start_offset = line_offset
                section_lines.append(line)
            elif not line.strip() and section_lines:
                section_lines.append(line)
            else:
                if not section_lines:
                    section_start_line = line_number
                    section_start_offset = line_offset
                section_lines.append(line)

        end_offset = fh.tell()

    if section_lines:
        chunk = _build_chunk(
            file_path.name, section_title, section_lines,
            section_start_line, line_number,
            section_start_offset, end_offset,
        )
        if chunk is not None:
            yield chunk


def _build_chunk(
    filename: str,
    section_title: str,
    lines: List[str],
    start_line: int,
    end_line: int,
    start_offset: int,
    end_offset: int,
) -> Document | None:
    """Собрать Document из накопленных строк секции. None если пустой контент."""
    content = "\n".join(lines).strip()
    if not content:
        return None
    return Document(
        page_content=content,
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
# Чтение контекста — извлечение фрагмента с расширением
# ---------------------------------------------------------------------------

def read_fragment(file_path: Path, hit: SearchHit, expand_lines: int) -> Fragment:
    """Прочитать фрагмент файла по позициям чанка + расширение на N строк.

    Возвращает Fragment с текстом и границами прочитанного диапазона.
    Использует те же текстовые позиции (tell/seek), что сохраняет iter_chunks.
    """
    loc = hit.location

    with open(file_path, encoding="utf-8", errors="replace") as fh:
        read_start_offset, read_start_line = _find_start_expanded(
            fh, loc.start_offset, loc.start_line, expand_lines,
        )

        fh.seek(read_start_offset)
        lines: list[str] = []
        current_line = read_start_line

        past_end_lines = 0
        while True:
            line = fh.readline()
            if not line:
                break
            lines.append(line.rstrip("\n").rstrip("\r"))
            current_line += 1
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
    """Найти позицию и номер строки на expand_lines раньше start_offset.

    Возвращает (offset, line_number).
    """
    if start_offset == 0 or expand_lines == 0:
        return start_offset, start_line

    fh.seek(0)
    # (offset, line_number) — кольцевой буфер последних позиций
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
