"""Унифицированный протокол для парсеров файл → `Chunk[str]`.

`FolderIndexer` ходит по дереву и диспатчит по расширению — каждый
парсер декларирует свои `extensions` + умеет `build_chunks_from_file`.
Md-парсер выдаёт один чанк на файл (pre-chunked .md), html-парсер
выдаёт N чанков (auto-split по заголовкам).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

from boba.indexing.chunks import Chunk

__all__ = ["ChunkParser"]


@runtime_checkable
class ChunkParser(Protocol):
    """Парсер документов одного формата в `Chunk[str]`."""

    extensions: ClassVar[tuple[str, ...]]
    """Расширения файлов (lowercase, с точкой), которые понимает парсер.

    Class-level: `FolderIndexer` диспатчит по `type(parser).extensions`,
    конкретные парсеры объявляют `extensions: ClassVar[tuple[str, ...]] = ...`.
    """

    def build_chunks_from_file(
        self, file_path: Path, *, root: Path,
    ) -> Iterable[Chunk[str]]:
        """Прочитать файл и выдать чанки.

        Реализация обязана:
        - читать UTF-8;
        - использовать `file_path.resolve().relative_to(root.resolve())`
          (posix) как `source_id` — тогда `chunk_id` стабилен между
          запусками для того же файла;
        - проставить `content_hash` для skip-if-unchanged логики
          `FolderIndexer`.
        """
        ...
