"""Tool `kb_search_hybrid` + `KbSearchHybridConfig`: vector + FTS + RRF поверх KB.

Self-contained tool-конфиг: содержит всё необходимое для построения
`PostgresKnowledgeBase` inline. LLM передаёт только `query` + опц.
`top_k`. Список целевых коллекций и потолок top_k пинятся оператором
в TOML-секции `[tool.kb.search.hybrid]`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList
from boba.tool.kb.core.errors import KnowledgeBaseError
from boba.tool.kb.core.kb import (
    PostgresKnowledgeBase,
    PostgresKnowledgeBaseConfig,
)
from boba.tools import FromConfig, tool

__all__ = ["KbSearchHybridConfig", "kb_search_hybrid"]

_DEFAULT_SQL_PATH = Path(__file__).parent / "sql" / "hybrid.sql"


class KbSearchHybridConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `kb_search_hybrid`.

    Config-секция: `[tool.kb.search.hybrid]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.search.hybrid",
        defaults_from=("postgres", "kb.storage", "embedding"),
    )

    knowledge_base: PostgresKnowledgeBaseConfig
    collections: StringList = Field(
        default_factory=lambda: ["kb_kbdoc", "kb_confluence"],
        description=(
            "Список коллекций (`collection` в `kb_chunks`), по объединению "
            "которых выполняется hybrid-search. SQL: "
            "`WHERE collection = ANY(%(collections)s)`. "
            'CSV в env (`"a,b"`) или TOML-array (`["a", "b"]`).'
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
            "Путь к файлу с hybrid-SQL (vector + FTS + RRF). По "
            "умолчанию — packaged `core/tools/search/sql/hybrid.sql`. "
            "Шаблон должен содержать identifier-placeholder'ы "
            "`{dim}`/`{chunks_table}`/`{schema}` и bind-параметры "
            "`%(collections|embedding|query|lang|rrf_k|rrf_pool|"
            "snippet_chars|top_k)s`."
        ),
    )


@tool
def kb_search_hybrid(
    cfg: Annotated[KbSearchHybridConfig, FromConfig()],
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
) -> list[dict[str, Any]]:
    """
    Hybrid semantic search (vector + FTS + RRF) по KB-коллекциям

    Возвращает JSON-массив hits, упорядоченный по релевантности
    (меньшее distance = ближе/релевантнее).
    Каждый hit — `{id, distance, link, metadata, snippet}`
    `distance` — отрицательный RRF-скор.
    """
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    kb = PostgresKnowledgeBase(cfg=cfg.knowledge_base)
    sql_template = cfg.search_sql_path.read_text(encoding="utf-8")
    try:
        hits = kb.search(
            collections=list(cfg.collections),
            query=query,
            top_k=top_k,
            sql_template=sql_template,
        )
    except KnowledgeBaseError as e:
        raise RuntimeError(str(e)) from e

    return [
        {
            "id": h.id,
            "distance": h.distance,
            "link": _build_link(h.metadata),
            "metadata": dict(h.metadata),
            "snippet": h.snippet,
        }
        for h in hits
    ]


def _build_link(metadata: Mapping[str, str]) -> str:
    """source_url[#anchor] — готовый deep-link, чтобы агент не склеивал сам."""
    url = str(metadata.get("source_url") or "")
    if not url:
        return ""
    anchor = str(metadata.get("anchor") or "")
    return f"{url}#{anchor}" if anchor else url
