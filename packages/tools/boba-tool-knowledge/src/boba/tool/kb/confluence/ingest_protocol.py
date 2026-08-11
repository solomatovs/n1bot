"""Контракт ingest-payload'а: весь прогон индексации идёт внутри песочницы."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.protocol import ConfluenceNode
from boba.toolkit.secrets import SecretDump

__all__ = ["ConfluenceIngestRequest", "IngestAnswer", "IngestMode"]


class IngestMode(StrEnum):
    """Способ обхода Confluence: чем задан список страниц прогона."""

    PAGES = "pages"
    CQL = "cql"
    SPACES = "spaces"


class ConfluenceIngestRequest(BaseModel):
    """Конфиг инструмента и способ обхода: остальное payload знает сам."""

    model_config = ConfigDict(extra="forbid")

    op: ConfluenceNode
    config: ConfluenceIngestConfig
    mode: IngestMode
    page_ids: Sequence[str] = ()
    cql: str = ""
    space_keys: Sequence[str] = ()
    prune_missing: bool
    force_update: bool

    @field_serializer("config", when_used="json")
    def _dump_config(self, value: ConfluenceIngestConfig) -> dict[str, Any]:
        """tool_args песочницы — доверенный канал: только здесь секреты раскрыты."""
        return SecretDump.of(value)


class IngestAnswer(BaseModel):
    """Статистика прогона: collection/indexed/skipped_unchanged/pruned/failed."""

    model_config = ConfigDict(extra="forbid")

    stats: dict[str, Any] = Field(description="Счётчики прогона как их дал pipeline.")
