"""PostgresFtsPlugin: точка регистрации PG-FTS read-tools."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar, Self

from pydantic import Field, model_validator

from boba.db.postgres import PostgresConfig, PostgresPool
from boba.plugin import ExtensionContext, Plugin
from boba.plugin.prompt import PromptOverlay
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict
from boba.tool.postgres_fts.db import PgFtsKnowledgeBase
from boba.tool.postgres_fts.fts_list_indexes import (
    FtsListIndexesTool,
    FtsListIndexesToolConfig,
)
from boba.tool.postgres_fts.fts_search import FtsSearchTool, FtsSearchToolConfig
from boba.tool.postgres_fts.models import IndexSpec
from boba.tools.domain import ToolSourceId
from boba.tools.framework import StaticToolSource, ToolSource

__all__ = ["PostgresFtsPlugin", "PostgresFtsPluginConfig"]


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
        boba_env_prefix="BOBA_TOOL__POSTGRES_FTS__",
        boba_toml_section="tool.postgres_fts",
    )

    enable: bool = Field(
        default=False,
        description="Подключить плагин в discovery.",
    )
    dsn: str = Field(
        default="",
        description=(
            "libpq DSN; read-only/statement_timeout задаются параметрами "
            "в самом DSN. Обязателен при enable=True."
        ),
    )
    indexes: list[IndexSpec] = Field(default_factory=list)
    min_pool_size: int = Field(
        default=1,
        ge=1,
        description="Минимальный размер pool'а.",
    )
    max_pool_size: int = Field(
        default=4,
        ge=1,
        description="Максимальный размер pool'а.",
    )
    connect_timeout_sec: float = Field(
        default=10.0,
        description="Таймаут получения connection из pool'а.",
    )
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
    fts_search: PromptOverlay = Field(default_factory=PromptOverlay)
    fts_list_indexes: PromptOverlay = Field(default_factory=PromptOverlay)

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


class PostgresFtsPlugin(Plugin[PostgresFtsPluginConfig, ToolSource]):
    """Plugin PG FTS read-tools: fts_search + fts_list_indexes."""

    NAME: ClassVar[str] = "postgres_fts"
    SOURCE_ID: ClassVar[ToolSourceId] = ToolSourceId("plugin_postgres_fts")

    @classmethod
    def build(
        cls,
        cfg: PostgresFtsPluginConfig,
        ctx: ExtensionContext,
    ) -> Iterable[ToolSource]:
        pool = PostgresPool.get(
            PostgresConfig(
                dsn=cfg.dsn,
                min_size=cfg.min_pool_size,
                max_size=cfg.max_pool_size,
                connect_timeout_sec=cfg.connect_timeout_sec,
            ),
        )
        kb = PgFtsKnowledgeBase(
            pool=pool,
            indexes=cfg.indexes,
            snippet_options=cfg.snippet_options,
        )
        sid = cls.SOURCE_ID
        yield StaticToolSource(
            source_id=sid,
            tools=[
                FtsSearchTool(
                    kb,
                    FtsSearchToolConfig(
                        max_top_k=cfg.max_top_k,
                        prompt=cfg.fts_search,
                    ),
                    ctx,
                    sid,
                ),
                FtsListIndexesTool(
                    kb,
                    FtsListIndexesToolConfig(prompt=cfg.fts_list_indexes),
                    ctx,
                    sid,
                ),
            ],
        )
