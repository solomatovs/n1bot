"""Адаптер подготовки документов для индексации.

Обрабатывает файлы из папки и складывает результат в output_dir:
- HTML (.html, .htm) → конвертирует в Markdown через markdownify
- Markdown (.md) → копирует как есть
- В будущем: другие форматы (.docx, .rst и т.д.)
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Iterator

from markdownify import markdownify as md

from domain.convert import (
    ConvertDone,
    ConvertEvent,
    ConvertFileDone,
    ConvertFileFailed,
    ConvertFileStarted,
)

log = logging.getLogger(__name__)

_HTML_EXTENSIONS = {".html", ".htm"}
_MARKDOWN_EXTENSIONS = {".md"}
_SUPPORTED_EXTENSIONS = _HTML_EXTENSIONS | _MARKDOWN_EXTENSIONS


class DocumentPreparer:
    """Подготавливает документы для индексации — конвертирует или копирует в output_dir."""

    def prepare_folder(
        self, source_dir: Path, output_dir: Path,
    ) -> Iterator[ConvertEvent]:
        """Обработать все поддерживаемые файлы из source_dir в output_dir."""
        file_count = _count_files(source_dir)
        if file_count == 0:
            yield ConvertDone(ok_count=0, failed_count=0)
            return

        output_dir.mkdir(parents=True, exist_ok=True)

        ok = 0
        failed = 0
        idx = 0

        for file_path in _iter_files(source_dir):
            idx += 1
            yield ConvertFileStarted(
                filename=file_path.name, index=idx, total=file_count,
            )
            try:
                target = _process_file(file_path, output_dir)
                ok += 1
                yield ConvertFileDone(
                    source=file_path.name,
                    target=target.name,
                    index=idx,
                    total=file_count,
                )
            except Exception as e:
                failed += 1
                log.warning("Failed to process %s: %s", file_path.name, e)
                yield ConvertFileFailed(
                    filename=file_path.name,
                    error=str(e),
                    index=idx,
                    total=file_count,
                )

        yield ConvertDone(ok_count=ok, failed_count=failed)


# ---------------------------------------------------------------------------
# Файловые итераторы
# ---------------------------------------------------------------------------

def _count_files(folder: Path) -> int:
    """Подсчитать количество обрабатываемых файлов (быстрый проход)."""
    if not folder.is_dir():
        return 0
    return sum(1 for f in folder.iterdir() if _is_supported(f))


def _iter_files(folder: Path) -> Iterator[Path]:
    """Лениво итерировать поддерживаемые файлы."""
    if not folder.is_dir():
        return
    for f in sorted(folder.iterdir(), key=lambda p: p.name):
        if _is_supported(f):
            yield f


def _is_supported(f: Path) -> bool:
    return f.is_file() and f.suffix.lower() in _SUPPORTED_EXTENSIONS and not f.name.startswith(".")


# ---------------------------------------------------------------------------
# Обработка отдельных файлов
# ---------------------------------------------------------------------------

def _process_file(file_path: Path, output_dir: Path) -> Path:
    """Обработать один файл. HTML → конвертировать, MD → скопировать."""
    suffix = file_path.suffix.lower()
    if suffix in _HTML_EXTENSIONS:
        return _convert_html_to_md(file_path, output_dir)
    if suffix in _MARKDOWN_EXTENSIONS:
        return _copy_markdown(file_path, output_dir)
    raise ValueError(f"Unsupported file extension: {suffix}")


def _convert_html_to_md(file_path: Path, output_dir: Path) -> Path:
    """Сконвертировать HTML-файл в Markdown."""
    html_content = file_path.read_text(encoding="utf-8", errors="replace")

    markdown_content = md(
        html_content,
        heading_style="ATX",
        strip=["script", "style"],
    )

    target_path = output_dir / file_path.with_suffix(".md").name
    target_path.write_text(markdown_content, encoding="utf-8")
    return target_path


def _copy_markdown(file_path: Path, output_dir: Path) -> Path:
    """Скопировать Markdown-файл как есть."""
    target_path = output_dir / file_path.name
    shutil.copy2(file_path, target_path)
    return target_path
