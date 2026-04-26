"""Reader для Markdown-файлов.

В v0.1 — без специальной обработки разметки: читаем как plain text.
Чанковщик (_chunking) старается резать по двойным переводам строк,
которые в md обычно соответствуют границам параграфов/секций.

Структурное разбиение по заголовкам (с metadata heading,
heading_level) — задел на будущее, добавим если выяснится, что
плоское чанкование плохо передаёт контекст.
"""

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
