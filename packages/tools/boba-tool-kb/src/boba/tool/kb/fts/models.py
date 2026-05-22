"""Value-objects PG-FTS: IndexSpec, FtsHit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["FtsHit", "IndexSpec"]


class IndexSpec(BaseModel):
    """Декларация одной FTS-таблицы оператора (`FtsSearchConfig.whitelist`).

    Имена колонок/таблицы приходят из конфига (TOML), не от LLM, и
    подставляются в SQL через `psycopg.sql.Identifier`. Параметры запроса
    (`query`, `top_k`) — всегда через placeholder'ы, без склейки.
    """

    # protected_namespaces=() — гасим предупреждение про field "schema",
    # которое shadow'ит deprecated v1-метод BaseModel.schema(); сам метод
    # в pydantic v2 заменён на model_json_schema().
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        protected_namespaces=(),
    )

    name: str = Field(description="Имя индекса (для логов/идентификации).")
    description: str = Field(description="Что лежит в этом индексе — для LLM.")
    table: str = Field(description="Имя таблицы (без schema).")
    id_column: str = Field(description="Колонка-PK; её значение возвращается в hit.id.")
    tsv_column: str = Field(
        description="Колонка типа tsvector (обычно GENERATED + GIN-индекс).",
    )
    snippet_column: str = Field(
        description="Текстовая колонка для ts_headline (обычно body/content).",
    )
    schema: str = Field(default="public", description="PG schema таблицы.")
    language: str = Field(
        default="english",
        description="PG search config (regconfig): russian/english/simple/...",
    )
    metadata_columns: list[str] = Field(
        default_factory=list,
        description=(
            "Колонки, отдаваемые как hit.metadata (например title, source_url)."
        ),
    )


@dataclass(frozen=True)
class FtsHit:
    """Один результат fts_search; score — `ts_rank_cd` (больше = релевантнее)."""

    id: str
    score: float
    metadata: Mapping[str, str]
    snippet: str
