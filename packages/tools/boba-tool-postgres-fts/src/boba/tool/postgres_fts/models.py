"""Value-objects PG-FTS: IndexSpec, IndexInfo, FtsHit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated

from boba.schema.coercion import ParseString

__all__ = ["FtsHit", "IndexInfo", "IndexSpec"]


@dataclass(frozen=True)
class IndexSpec:
    """Декларация одного FTS-индекса в whitelist'е плагина.

    Имена колонок/таблицы приходят из конфига (TOML), не от LLM, и
    подставляются в SQL через `psycopg.sql.Identifier`. Параметры запроса
    (`query`, `top_k`) — всегда через placeholder'ы, без склейки.
    """

    name: Annotated[
        str,
        "Имя индекса для агента (видно в fts_list_indexes).",
        ParseString(),
    ]
    description: Annotated[
        str,
        "Что лежит в этом индексе — для LLM.",
        ParseString(),
    ]
    table: Annotated[
        str,
        "Имя таблицы (без schema).",
        ParseString(),
    ]
    id_column: Annotated[
        str,
        "Колонка-PK; её значение возвращается в hit.id.",
        ParseString(),
    ]
    tsv_column: Annotated[
        str,
        "Колонка типа tsvector (обычно GENERATED + GIN-индекс).",
        ParseString(),
    ]
    snippet_column: Annotated[
        str,
        "Текстовая колонка для ts_headline (обычно body/content).",
        ParseString(),
    ]
    schema: Annotated[
        str,
        "PG schema таблицы.",
        ParseString(),
    ] = "public"
    language: Annotated[
        str,
        "PG search config (regconfig): russian/english/simple/...",
        ParseString(),
    ] = "english"
    metadata_columns: Annotated[
        list[str],
        "Колонки, отдаваемые как hit.metadata (например title, source_url).",
    ] = field(default_factory=list)


@dataclass(frozen=True)
class IndexInfo:
    """Краткое описание индекса для fts_list_indexes."""

    name: str
    description: str


@dataclass(frozen=True)
class FtsHit:
    """Один результат fts_search; score — `ts_rank_cd` (больше = релевантнее)."""

    id: str
    score: float
    metadata: Mapping[str, str]
    snippet: str
