"""Reader для plain text файлов."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from boba.cli.vector_index.readers.base import Document


class TextReader:
    extensions: tuple[str, ...] = (".txt",)

    def read(self, path: str) -> Iterable[Document]:
        text = Path(path).read_text(encoding="utf-8")
        yield Document(
            source_path=str(Path(path).resolve()),
            text=text,
            metadata={"format": "text"},
        )
