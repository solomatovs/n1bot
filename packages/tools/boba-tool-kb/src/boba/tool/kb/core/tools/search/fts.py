"""Tool `kb_search_fts` + `KbSearchFtsConfig`: pure FTS (ts_rank_cd) поверх KB.

Параллелен `kb_search_vector` (cosine) — только FTS-канал. Полезен для точных
лексических совпадений (имена, идентификаторы, фразы) и для коротких
ключевых запросов, где embedding-канал шумит. LLM передаёт только
`query` + опц. `top_k`.

Read-side `kb_chunks`: каждый hit — `{id, distance, link, metadata,
snippet}`, где `id` = `chunk_id`, `snippet` = `format_content`, а
`metadata` — ровно `kb_chunks.metadata` jsonb матчнутого чанка (плоский
`dict[str, str]`; набор ключей зависит от коллекции). Пример заполнения
ключей — в ingest-tool'ах (`...kbdoc.tools.ingest`,
`...confluence.tools.ingest`).
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

__all__ = ["KbSearchFtsConfig", "kb_search_fts"]

_DEFAULT_SQL_PATH = Path(__file__).parent / "sql" / "fts.sql"


class KbSearchFtsConfig(BobaFlatSettings):
    """Self-contained конфиг tool'а `kb_search_fts`.

    Config-секция: `[tool.kb.search.fts]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.search.fts",
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
            "Список коллекций для pure FTS-search. SQL: "
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
        ),
    )


@tool
def kb_search_fts(
    cfg: Annotated[KbSearchFtsConfig, FromConfig()],
    query: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Поисковый запрос. По умолчанию пробел = AND (все слова должны "
                'встретиться). Операторы: `OR` — альтернативы, `"точная фраза"` — '
                "фраза целиком, `-слово` — исключить. Многословный запрос лучше "
                "разбавлять `OR` (`pix OR adqm OR учётка`), иначе AND по всем "
                "словам часто даёт ноль."
            ),
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
    """Lexical full-text search по KB-коллекциям (`ts_rank_cd`).

    Запрос разбирается как websearch: пробел = AND, `OR` = альтернативы,
    `"фраза"` = точная фраза, `-слово` = исключение. Возвращает JSON-массив hits
    `{id, distance, link, metadata, snippet}`, упорядоченный по релевантности
    (меньше distance = ближе). `distance` — отрицательный `ts_rank_cd`.
    """
    if top_k > cfg.max_top_k:
        raise RuntimeError(
            f"top_k={top_k} превышает max_top_k={cfg.max_top_k}",
        )
    kb = PostgresKnowledgeBase(cfg=cfg.knowledge_base)
    sql_template = cfg.search_sql_path.read_text(encoding="utf-8")
    try:
        return [
            {
                "id": h.id,
                "distance": h.distance,
                "link": _build_link(h.metadata),
                "metadata": dict(h.metadata),
                "snippet": h.snippet,
            }
            for h in kb.fts_search(
                collections=list(cfg.collections),
                query=query,
                top_k=top_k,
                sql_template=sql_template,
            )
        ]
    except KnowledgeBaseError as e:
        raise RuntimeError(str(e)) from e


def _build_link(metadata: Mapping[str, str]) -> str:
    """source_url[#anchor] — готовый deep-link, чтобы агент не склеивал сам."""
    url = str(metadata.get("source_url") or "")
    if not url:
        return ""
    anchor = str(metadata.get("anchor") or "")
    return f"{url}#{anchor}" if anchor else url
