"""Tools `kb_confluence_search` / `kb_confluence_doc_search` — KB-поиск.

Единый интерфейс: параметр `method` (`vector` | `fts`) выбирает канал поиска.
Каждый tool строго привязан к ОДНОЙ коллекции через дискриминирующий тип
(`ConfluenceCollection` / `KbDocCollection`) — строгий фильтр по `collection`
даёт детерминированную сборку metadata (см. `schema.py`), без `if/else`.

Общий конфиг-секция: `[tool.kb.search]` (knowledge_base / max_top_k / пути к
SQL-шаблонам vector|fts).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.kb.core.errors import KnowledgeBaseError
from boba.tool.kb.core.kb import (
    PostgresKnowledgeBase,
    PostgresKnowledgeBaseConfig,
)
from boba.tool.kb.core.tools.search.schema import (
    CollectionSearch,
    ConfluenceCollection,
    KbDocCollection,
)
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult

__all__ = ["KbSearchConfig", "kb_confluence_doc_search", "kb_confluence_search"]

_SQL_DIR = Path(__file__).parent / "sql"

SearchMethod = Literal["vector", "fts"]

_QUERY_DESC = (
    "Поисковый запрос. Для `method=vector` — естественный язык (семантика). "
    'Для `method=fts` — websearch-синтаксис: пробел = AND, `OR` = альтернативы, '
    '`"фраза"` = фраза целиком, `-слово` = исключить.'
)
_METHOD_DESC = (
    "Канал поиска: `vector` — семантический (cosine-эмбеддинги, синонимы, "
    "длинные запросы); `fts` — лексический (`ts_rank_cd`, точные слова/имена/"
    "идентификаторы). По умолчанию `vector`."
)
_TOPK_DESC = "Сколько hits вернуть. По умолчанию 5."


class KbSearchConfig(BobaFlatSettings):
    """Общий конфиг search-tool'ов. Секция `[tool.kb.search]`."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.search",
        defaults_from=(
            "kb.storage",
            "postgres.{kb.storage:profile}",
            "embedding",
        ),
    )

    knowledge_base: PostgresKnowledgeBaseConfig
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра `top_k`.",
    )
    vector_sql_path: Path = Field(
        default_factory=lambda: _SQL_DIR / "vector.sql",
        description="SQL-шаблон vector-канала (по умолчанию `sql/vector.sql`).",
    )
    fts_sql_path: Path = Field(
        default_factory=lambda: _SQL_DIR / "fts.sql",
        description="SQL-шаблон fts-канала (по умолчанию `sql/fts.sql`).",
    )


def _search(
    cfg: KbSearchConfig,
    collection: type[CollectionSearch],
    query: str,
    method: SearchMethod,
    top_k: int,
) -> TableResult:
    """Единый прогон: `method` выбирает канал, `collection` — scope и сборку строк."""
    if top_k > cfg.max_top_k:
        raise RuntimeError(f"top_k={top_k} превышает max_top_k={cfg.max_top_k}")
    kb = PostgresKnowledgeBase(cfg=cfg.knowledge_base)
    try:
        if method == "vector":
            sql_template = cfg.vector_sql_path.read_text(encoding="utf-8")
            hits = kb.vector_search(
                collections=[collection.COLLECTION],
                query=query,
                top_k=top_k,
                sql_template=sql_template,
            )
        else:
            sql_template = cfg.fts_sql_path.read_text(encoding="utf-8")
            hits = kb.fts_search(
                collections=[collection.COLLECTION],
                query=query,
                top_k=top_k,
                sql_template=sql_template,
            )
        rows = [collection.row(h) for h in hits]
    except KnowledgeBaseError as e:
        raise RuntimeError(str(e)) from e
    return TableResult(rows=rows, note=None if rows else "ничего не найдено")


@tool
def kb_confluence_search(
    cfg: Annotated[KbSearchConfig, FromConfig()],
    query: Annotated[str, Field(min_length=1, description=_QUERY_DESC)],
    method: Annotated[SearchMethod, Field(description=_METHOD_DESC)] = "vector",
    top_k: Annotated[int, Field(ge=1, description=_TOPK_DESC)] = 5,
) -> TableResult:
    """Поиск по коллекции Confluence-страниц (`kb_confluence`).

    Канал выбирается параметром `method` (`vector`|`fts`). Возвращает
    `TableResult` — плоскую таблицу hits с колонками `id`/`distance`/`link`/
    `snippet` + `page_title`/`source_url`/`anchor`/`page_id`/`heading_path`/
    `space`/`tags`, по релевантности (меньше `distance` = ближе).
    """
    return _search(cfg, ConfluenceCollection, query, method, top_k)


@tool
def kb_confluence_doc_search(
    cfg: Annotated[KbSearchConfig, FromConfig()],
    query: Annotated[str, Field(min_length=1, description=_QUERY_DESC)],
    method: Annotated[SearchMethod, Field(description=_METHOD_DESC)] = "vector",
    top_k: Annotated[int, Field(ge=1, description=_TOPK_DESC)] = 5,
) -> TableResult:
    """Поиск по коллекции загруженных KbDoc-выгрузок Confluence (`kb_confluence_doc`).

    Канал выбирается параметром `method` (`vector`|`fts`). Тот же формат
    `TableResult`, что и у `kb_confluence_search`.
    """
    return _search(cfg, KbDocCollection, query, method, top_k)
