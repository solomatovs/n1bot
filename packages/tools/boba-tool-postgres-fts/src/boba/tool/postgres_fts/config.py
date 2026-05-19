"""Конфиг плагина postgres_fts (v2).

Один `BobaFlatSettings`, секция `[tool.postgres_fts]`, env-prefix
`BOBA_TOOL__POSTGRES_FTS__`. Используется и tools (через FromConfig),
и provider'ами PostgresPool/PgFtsKnowledgeBase.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList
from boba.tool.postgres_fts.models import IndexSpec

__all__ = ["PostgresFtsPluginConfig"]


class PostgresFtsPluginConfig(BobaFlatSettings):
    """PG FTS read-tools: fts_search + fts_list_indexes.

    `dsn` — libpq-строка; для read-only обязательно добавьте параметры
    `default_transaction_read_only=on&statement_timeout=<ms>` прямо в DSN
    (см. boba-db-postgres). Whitelist индексов задаётся через `indexes` —
    LLM видит только то, что описано здесь.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        config_path="tool.postgres_fts",
    )

    enable: bool = Field(
        default=False,
        description="Регистрировать ли FTS-tools в DI/каталоге LLM.",
    )
    tools: StringList | None = Field(
        default=None,
        description=(
            "Allowlist tool-имён: None — оба включены; иначе только "
            "перечисленные ('fts_search', 'fts_list_indexes')."
        ),
    )
    dsn: str = Field(
        default="",
        description=(
            "libpq DSN; read-only/statement_timeout задаются параметрами "
            "в самом DSN. Обязателен при enable=True."
        ),
    )
    indexes: list[IndexSpec] = Field(default_factory=list)
    min_pool_size: int = Field(default=1, ge=1)
    max_pool_size: int = Field(default=4, ge=1)
    connect_timeout_sec: float = Field(default=10.0)
    snippet_options: str = Field(
        default="MaxFragments=2,MaxWords=35,MinWords=15",
        description=(
            "Опции ts_headline: MaxFragments,MaxWords,MinWords,StartSel,StopSel,..."
        ),
    )
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра top_k для fts_search.",
    )

    @model_validator(mode="after")
    def _check_when_enabled(self) -> Self:
        if not self.enable:
            return self
        if not self.dsn:
            msg = "dsn обязателен при enable=True"
            raise ValueError(msg)
        if not self.indexes:
            msg = "indexes должен содержать хотя бы один IndexSpec при enable=True"
            raise ValueError(msg)
        return self
