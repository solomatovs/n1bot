"""Стратегия чтения HTML-документов.

Разбиение по заголовкам (<h1>–<h6>). Каждая секция — чанк.
page_content = чистый текст (для embedding), метаданные = позиции в оригинальном HTML.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List

from langchain_core.documents import Document

from boba_app.readers.registry import DocumentReader, build_chunk

_HEADING_OPEN = re.compile(r"<h([1-6])[\s>]", re.IGNORECASE)
_TAG_STRIP = re.compile(r"<[^>]+>")


class HtmlReader(DocumentReader):
    """Стратегия: HTML (.html, .htm)."""

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".html", ".htm"})

    def iter_chunks(self, file_path: Path) -> Iterator[Document]:
        """Разбить HTML на секции по h1–h6.

        page_content = plain text (для embedding).
        metadata = позиции в оригинальном HTML.
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
                stripped = line.rstrip("\n").rstrip("\r")

                heading_match = _HEADING_OPEN.search(stripped)
                if heading_match:
                    if section_lines:
                        chunk = _build_html_chunk(
                            file_path.name,
                            section_title,
                            section_lines,
                            section_start_line,
                            line_number - 1,
                            section_start_offset,
                            line_offset,
                        )
                        if chunk is not None:
                            yield chunk
                        section_lines = []

                    section_title = _extract_heading_text(stripped)
                    section_start_line = line_number
                    section_start_offset = line_offset

                if not section_lines and not heading_match:
                    section_start_line = line_number
                    section_start_offset = line_offset

                section_lines.append(stripped)

            end_offset = fh.tell()

        if section_lines:
            chunk = _build_html_chunk(
                file_path.name,
                section_title,
                section_lines,
                section_start_line,
                line_number,
                section_start_offset,
                end_offset,
            )
            if chunk is not None:
                yield chunk


# ---------------------------------------------------------------------------
# Приватные хелперы
# ---------------------------------------------------------------------------


def _build_html_chunk(
    filename: str,
    section_title: str,
    lines: List[str],
    start_line: int,
    end_line: int,
    start_offset: int,
    end_offset: int,
) -> Document | None:
    """Собрать Document. page_content = plain text (без тегов)."""
    html_content = "\n".join(lines)
    text = _html_to_text(html_content)
    return build_chunk(
        filename,
        section_title,
        text,
        start_line,
        end_line,
        start_offset,
        end_offset,
    )


def _extract_heading_text(line: str) -> str:
    """<h2>Title</h2> → Title."""
    return _TAG_STRIP.sub("", line).strip()


def _html_to_text(html: str) -> str:
    """Грубое извлечение текста: убрать теги, нормализовать пробелы."""
    text = _TAG_STRIP.sub(" ", html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
