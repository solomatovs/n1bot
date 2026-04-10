"""Стратегия чтения Markdown-документов.

Разбиение по заголовкам (# / ## / ###). Каждая секция — чанк.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List

from langchain_core.documents import Document

from application.doc_pipeline.doc_reader import DocumentReader, build_chunk

_HEADING_PATTERN = re.compile(r"^(#+)\s+(.+)$")


class MarkdownReader(DocumentReader):
    """Стратегия: Markdown (.md, .txt)."""

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset({".md", ".txt"})

    def iter_chunks(self, file_path: Path) -> Iterator[Document]:
        """Разбить Markdown на секции по заголовкам (#)."""
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
                        chunk = build_chunk(
                            file_path.name, section_title,
                            "\n".join(section_lines),
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
            chunk = build_chunk(
                file_path.name, section_title,
                "\n".join(section_lines),
                section_start_line, line_number,
                section_start_offset, end_offset,
            )
            if chunk is not None:
                yield chunk
