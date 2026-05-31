"""Tool `kb_search_vector` + `KbSearchVectorConfig`: pure vector (cosine) поверх KB.

Параллелен `kb_search_fts` (pure FTS) — только vector-канал. Полезен,
когда FTS-канал шумит/мешает (короткие запросы, эмбеддинг лучше ловит
синонимы).

LLM передаёт только `query` + опц. `top_k`.

Возвращает `TableResult` — плоскую таблицу, по строке на hit. Колонки:
служебные `id` / `distance` / `link` / `snippet` + `page_title` / `source_url`
/ `anchor` / `page_id` / `heading_path` / `space` + `tags`. Оба loader'а
(confluence и kbdoc) пишут эти поля под одинаковыми wire-ключами, поэтому
`search.llm_view` читает их 1:1 без сведе́ния источников.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList
from boba.tool.kb.core.errors import KnowledgeBaseError
from boba.tool.kb.core.kb import (
    PostgresKnowledgeBase,
    PostgresKnowledgeBaseConfig,
)
from boba.tool.kb.core.tools.search.llm_view import flat_row
from boba.tools import FromConfig, tool
from boba.tools.domain import TableResult

__all__ = ["KbSearchVectorConfig", "kb_search_vector"]

_DEFAULT_SQL_PATH = Path(__file__).parent / "sql" / "vector.sql"


class KbSearchVectorConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `kb_search_vector`.

    Config-секция: `[tool.kb.search.vector]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.search.vector",
        defaults_from=(
            "kb.storage",
            "postgres.{kb.storage:profile}",
            "embedding",
        ),
    )

    knowledge_base: PostgresKnowledgeBaseConfig
    collections: StringList = Field(
        default_factory=lambda: ["kb_kbdoc", "kb_confluence"],
        description=(
            "Список коллекций для pure vector-search. SQL: "
            "`WHERE collection = ANY(%(collections)s)`."
        ),
    )
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра `top_k`.",
    )
    search_sql_path: Path = Field(
        default_factory=lambda: _DEFAULT_SQL_PATH,
        description=(
            "Путь к файлу-шаблону sql запроса"
            "умолчанию — packaged `core/tools/search/sql/vector.sql`. "
        ),
    )


@tool
def kb_search_vector(
    cfg: Annotated[KbSearchVectorConfig, FromConfig()],
    query: Annotated[
        str,
        Field(
            min_length=1,
            description="Поисковый запрос на естественном языке"
        ),
    ],
    top_k: Annotated[
        int,
        Field(
            ge=1,
            description=(
                "Сколько hits вернуть. По умолчанию 5"
            ),
        ),
    ] = 5,
) -> TableResult:
    """semantic search по KB-коллекциям

    Возвращает `TableResult` — плоскую таблицу hits (по строке на чанк) с
    колонками `id`/`distance`/`link`/`snippet` + `llm.*`-ключи, упорядоченную
    по релевантности (меньше distance = ближе).
    """
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    kb = PostgresKnowledgeBase(cfg=cfg.knowledge_base)
    sql_template = cfg.search_sql_path.read_text(encoding="utf-8")
    try:
        rows = [
            flat_row(h)
            for h in kb.vector_search(
                collections=list(cfg.collections),
                query=query,
                top_k=top_k,
                sql_template=sql_template,
            )
        ]
    except KnowledgeBaseError as e:
        raise RuntimeError(str(e)) from e
    note = None if rows else "ничего не найдено"
    return TableResult(rows=rows, note=note)
