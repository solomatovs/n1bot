"""Адаптер конвертации HTML-файлов в Markdown.

Читает HTML-файлы из папки, конвертирует через markdownify
и записывает .md файлы рядом с исходными.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, List

from markdownify import markdownify as md

from domain.convert import (
    ConvertDone,
    ConvertEvent,
    ConvertFileDone,
    ConvertFileFailed,
    ConvertFileStarted,
)

log = logging.getLogger(__name__)

# Расширения файлов, поддерживаемых для конвертации.
# В будущем можно добавить другие форматы (.docx, .rst и т.д.).
SUPPORTED_EXTENSIONS = {".html", ".htm"}


class HtmlToMarkdownConverter:
    """Конвертирует HTML-файлы в Markdown."""

    def convert_folder(self, folder: Path) -> Iterator[ConvertEvent]:
        """Сконвертировать все поддерживаемые файлы в папке."""
        files = _collect_convertible_files(folder)
        if not files:
            yield ConvertDone(ok_count=0, failed_count=0)
            return

        total = len(files)
        ok = 0
        failed = 0

        for idx, file_path in enumerate(files, start=1):
            yield ConvertFileStarted(
                filename=file_path.name, index=idx, total=total,
            )
            try:
                target = _convert_html_to_md(file_path)
                ok += 1
                yield ConvertFileDone(
                    source=file_path.name,
                    target=target.name,
                    index=idx,
                    total=total,
                )
            except Exception as e:
                failed += 1
                log.warning("Failed to convert %s: %s", file_path.name, e)
                yield ConvertFileFailed(
                    filename=file_path.name,
                    error=str(e),
                    index=idx,
                    total=total,
                )

        yield ConvertDone(ok_count=ok, failed_count=failed)


def _collect_convertible_files(folder: Path) -> List[Path]:
    """Собрать файлы с поддерживаемыми расширениями."""
    if not folder.is_dir():
        return []
    return sorted(
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def _convert_html_to_md(file_path: Path) -> Path:
    """Сконвертировать один HTML-файл в Markdown. Возвращает путь к .md файлу."""
    html_content = file_path.read_text(encoding="utf-8", errors="replace")

    markdown_content = md(
        html_content,
        heading_style="ATX",
        strip=["script", "style"],
    )

    target_path = file_path.with_suffix(".md")
    target_path.write_text(markdown_content, encoding="utf-8")
    return target_path
