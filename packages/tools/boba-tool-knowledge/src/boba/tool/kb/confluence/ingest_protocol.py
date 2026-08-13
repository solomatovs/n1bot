"""Контракт ingest-payload'а: весь прогон индексации идёт внутри песочницы."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.protocol import ConfluenceNode
from boba.toolkit.secrets import SecretDump
from boba.toolkit.types import LLMStringList

__all__ = [
    "ConfluenceIngestRequest",
    "IngestAnswer",
    "IngestMode",
    "IngestSource",
]


class IngestMode(StrEnum):
    """Способ обхода Confluence: чем задан список страниц прогона."""

    PAGES = "pages"
    CQL = "cql"
    SPACES = "spaces"

    def field(self) -> str:
        """Имя поля источника, которым режим задаёт страницы прогона."""
        if self is IngestMode.PAGES:
            return "page_ids"

        if self is IngestMode.CQL:
            return "cql"

        return "space_keys"


class IngestSource(BaseModel):
    """Источник страниц прогона: режим и заполненное поле своего режима."""

    model_config = ConfigDict(extra="forbid")

    mode: IngestMode
    page_ids: LLMStringList = Field(default_factory=list)
    cql: str = ""
    space_keys: LLMStringList = Field(default_factory=list)

    @model_validator(mode="after")
    def _only_field_of_its_mode(self) -> Self:
        """Ровно одно поле источника непусто, и это поле выбранного режима."""
        filled: list[str] = []

        if self.page_ids:
            filled.append(IngestMode.PAGES.field())

        if self.cql:
            filled.append(IngestMode.CQL.field())

        if self.space_keys:
            filled.append(IngestMode.SPACES.field())

        own = self.mode.field()

        if filled == [own]:
            return self

        given = ", ".join(filled)
        if not given:
            given = "none"

        msg = (
            f"mode={self.mode.value} takes its pages from {own} only, "
            f"so {own} must be set and the other source fields left empty; "
            f"non-empty source fields: {given}"
        )
        raise ValueError(msg)

    @classmethod
    def of(cls, mode: IngestMode, target: str) -> Self:
        """Источник из одной строки: списки — через запятую, cql — как есть."""
        raw: dict[str, Any] = {"mode": mode, mode.field(): target}

        return cls.model_validate(raw)

    def note(self) -> str:
        """Пометка результата: чем задан набор страниц прогона."""
        if self.mode is IngestMode.PAGES:
            return self._listed(self.page_ids)

        if self.mode is IngestMode.SPACES:
            return self._listed(self.space_keys)

        return f"{IngestMode.CQL.field()}: {self.cql}"

    def _listed(self, values: Sequence[str]) -> str:
        joined = ", ".join(values)

        return f"{self.mode.field()} ({len(values)}): {joined}"


class ConfluenceIngestRequest(BaseModel):
    """Конфиг инструмента и источник страниц: остальное payload знает сам."""

    model_config = ConfigDict(extra="forbid")

    op: ConfluenceNode
    config: ConfluenceIngestConfig
    source: IngestSource
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
