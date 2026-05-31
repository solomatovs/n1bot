"""Сборка плоской search-строки из канонических metadata-ключей.

Все KB-данные — выгрузки Confluence, поэтому оба loader'а (confluence и kbdoc)
пишут ОДИНАКОВЫЕ wire-ключи: `reader.page_title`, `source_url`,
`section.anchor`, `confluence.page_id`, `section.heading.path`,
`confluence.space_key`. Search читает их строго 1:1 (одна колонка = один
wire-ключ, без `??`-сведе́ния источников), переименовывая в плоские
output-колонки. Дополнительно — `tags` (колонка `kb_chunks.tags`) и готовый
deep-link `source_url[#anchor]`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from boba.indexing import ReaderKeys, SectionKeys
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.core.models import SearchHit

__all__ = ["build_link", "flat_row", "project_columns"]

# output-колонка → исходный wire-ключ (общий для confluence и kbdoc)
_COLUMNS: dict[str, str] = {
    "page_title": ReaderKeys.PAGE_TITLE.name,       # reader.page_title
    "source_url": ConfluenceKeys.SOURCE_URL.name,   # source_url
    "anchor": SectionKeys.ANCHOR.name,              # section.anchor
    "page_id": ConfluenceKeys.PAGE_ID.name,         # confluence.page_id
    "heading_path": SectionKeys.HEADING_PATH.name,  # section.heading.path
    "space": ConfluenceKeys.SPACE_KEY.name,         # confluence.space_key
}


def project_columns(hit: SearchHit) -> dict[str, str]:
    """Канонические колонки (1:1 из wire-ключей) + `tags` из колонки.

    Отсутствующий ключ → `""`, чтобы таблица оставалась равномерной.
    """
    cols = {col: hit.metadata.get(wire, "") for col, wire in _COLUMNS.items()}
    cols["tags"] = ", ".join(sorted(hit.tags))
    return cols


def build_link(columns: Mapping[str, str]) -> str:
    """`source_url[#anchor]` — готовый deep-link, чтобы агент не склеивал сам."""
    url = columns.get("source_url", "")
    if not url:
        return ""
    anchor = columns.get("anchor", "")
    if anchor and "#" not in url:
        return f"{url}#{anchor}"
    return url


def flat_row(hit: SearchHit) -> dict[str, Any]:
    """Плоская строка таблицы: служебные поля + канонические колонки."""
    columns = project_columns(hit)
    return {
        "id": hit.id,
        "distance": hit.distance,
        "link": build_link(columns),
        "snippet": hit.snippet,
        **columns,
    }
