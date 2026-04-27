"""Reader для Markdown-файлов; в v0.1 — как plain text."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from boba.cli.vector_index.readers.base import Document


class MarkdownReader:
    extensions: tuple[str, ...] = (".md", ".markdown")

    def read(self, path: str) -> Iterable[Document]:
        text = Path(path).read_text(encoding="utf-8")
        yield Document(
            source_path=str(Path(path).resolve()),
            text=text,
            metadata={"format": "markdown"},
        )
