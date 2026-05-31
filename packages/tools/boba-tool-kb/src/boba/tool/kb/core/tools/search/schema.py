"""Дискриминирующие типы поиска по коллекции — явная сборка строки из kb_chunks.

Каждый подкласс `CollectionSearch` фиксирует ОДНУ коллекцию (`COLLECTION` —
значение strict-фильтра по `kb_chunks.collection`) и ЯВНО перечисляет, какие
поля он собирает в строку результата:

- прямые колонки `kb_chunks` (одинаковы для всех коллекций) — собираются в
  базовом `row()`: `chunk_id` → `id`, `format_content` (срез) → `snippet`,
  `tags` (text[]) → `tags`, плюс relevance `distance`;
- поля из `kb_chunks.metadata` (jsonb) — задаются пер-типом в `META_FIELDS`
  ТИПИЗИРОВАННЫМИ `MetadataKey`-константами, причём ИМЕННО ТЕМИ, что пишет в
  metadata ingest этой коллекции. За счёт ссылки на одни и те же константы
  search-сторона ↔ ingest-сторона не расходятся.

Wire-имена ключей у обеих коллекций совпадают (kbdoc выровнен под confluence —
см. `KbDocKeys` / `ConfluenceKeys`), поэтому строки выходят одинаковой формы;
но каждый тип ссылается на константы СВОЕГО ingest, что и есть явное
пересечение «search читает то, что ingest записал».
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.indexing import MetadataKey, ReaderKeys, SectionKeys
from boba.kbdoc import KbDocKeys
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.core.models import SearchHit

__all__ = ["CollectionSearch", "ConfluenceCollection", "KbDocCollection", "MetaField"]


@dataclass(frozen=True)
class MetaField:
    """Output-колонка ← ключ `kb_chunks.metadata` (та `MetadataKey`, что у ingest)."""

    column: str
    key: MetadataKey[str]


class CollectionSearch:
    """База дискриминатора: строгий `COLLECTION` + явная сборка строки из kb_chunks.

    Подкласс обязан задать `COLLECTION` (scope) и `META_FIELDS` (поля из
    `metadata`). Прямые колонки `kb_chunks` собираются здесь, в `row()`.
    """

    COLLECTION: ClassVar[str]
    META_FIELDS: ClassVar[tuple[MetaField, ...]]

    @classmethod
    def row(cls, hit: SearchHit) -> dict[str, Any]:
        """Плоская строка результата: прямые колонки kb_chunks + поля metadata."""
        row: dict[str, Any] = {
            # --- прямые колонки kb_chunks ---
            "id": hit.id,                          # kb_chunks.chunk_id
            "distance": hit.distance,              # cosine / -ts_rank_cd
            "snippet": hit.snippet,                # kb_chunks.format_content (срез)
            "tags": ", ".join(sorted(hit.tags)),   # kb_chunks.tags (text[])
        }
        # --- поля из kb_chunks.metadata (jsonb), по META_FIELDS ---
        for field in cls.META_FIELDS:
            row[field.column] = hit.metadata.get(field.key.name, "")
        # --- производное ---
        row["link"] = cls._link(row)
        return row

    @staticmethod
    def _link(row: Mapping[str, str]) -> str:
        """`source_url[#anchor]` — готовый deep-link из уже собранных колонок."""
        url = row.get("source_url", "")
        if not url:
            return ""
        anchor = row.get("anchor", "")
        if anchor and "#" not in url:
            return f"{url}#{anchor}"
        return url


class ConfluenceCollection(CollectionSearch):
    """Коллекция Confluence-страниц (прямой HTTP-ingest).

    `META_FIELDS` ссылается на ключи, которые пишет confluence-ingest:
    request_source (`ConfluenceKeys.SOURCE_URL`/`PAGE_ID`), decoder
    (`ConfluenceKeys.SPACE_KEY`, `ReaderKeys.PAGE_TITLE`), reader
    (`SectionKeys.ANCHOR`/`HEADING_PATH`).
    """

    COLLECTION = "kb_confluence"

    META_FIELDS = (
        MetaField("page_title", ReaderKeys.PAGE_TITLE),       # decoder
        MetaField("source_url", ConfluenceKeys.SOURCE_URL),   # request_source
        MetaField("anchor", SectionKeys.ANCHOR),              # reader
        MetaField("page_id", ConfluenceKeys.PAGE_ID),         # request_source
        MetaField("heading_path", SectionKeys.HEADING_PATH),  # reader
        MetaField("space", ConfluenceKeys.SPACE_KEY),         # decoder
    )


class KbDocCollection(CollectionSearch):
    """Коллекция загруженных KbDoc-выгрузок Confluence (workspace upload).

    `META_FIELDS` ссылается на ключи, которые пишет kbdoc-ingest: `KbDocReader`
    из header'а (`KbDocKeys.SOURCE_URL`/`PAGE_ID`/`SPACE`, `ReaderKeys.
    PAGE_TITLE`, `SectionKeys.ANCHOR`) + `StructuralChunker`
    (`SectionKeys.HEADING_PATH`). Wire-имена совпадают с confluence-коллекцией.
    """

    COLLECTION = "kb_confluence_doc"

    META_FIELDS = (
        MetaField("page_title", ReaderKeys.PAGE_TITLE),       # KbDocReader (title:)
        MetaField("source_url", KbDocKeys.SOURCE_URL),        # KbDocReader (source:)
        MetaField("anchor", SectionKeys.ANCHOR),              # KbDocReader (anchor:)
        MetaField("page_id", KbDocKeys.PAGE_ID),              # KbDocReader (page_id:)
        MetaField("heading_path", SectionKeys.HEADING_PATH),  # StructuralChunker
        MetaField("space", KbDocKeys.SPACE),                  # KbDocReader (space:)
    )
