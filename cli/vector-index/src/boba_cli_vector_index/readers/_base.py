"""Контракты для reader'ов разных форматов.

Reader превращает источник (файл, URL, страница Confluence) в один или
несколько :class:`Document`. Reader'ам не положено знать о чанковании
или векторном хранилище — это делает :mod:`boba_cli_vector_index._indexer`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Document:
    """Логическая единица для индексации до чанкования.

    ``source_path`` — стабильный идентификатор источника, по которому
    делается dedupe в :mod:`boba_cli_vector_index._indexer`. Для файла
    это абсолютный путь; для будущих HTML/Confluence — URL/page-id.

    ``metadata`` — произвольные строковые поля, попадут в metadata
    каждого чанка (вместе с автоматически добавляемыми ``chunk_index``
    и ``source_path``).
    """

    source_path: str
    text: str
    metadata: dict[str, str] = field(default_factory=dict)


class Reader(Protocol):
    """Протокол reader'а: extension → ``Iterable[Document]``."""

    extensions: tuple[str, ...]

    def read(self, path: str) -> Iterable[Document]: ...
