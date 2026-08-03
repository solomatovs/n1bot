"""Вызов ingest-payload'а: индексация целиком идёт в песочнице."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boba.chainlit2.sandbox import SandboxCaller, SandboxEntryConfig

__all__ = ["ConfluenceIngestCaller", "IngestAnswer", "IngestRequest"]


class IngestRequest(BaseModel):
    """Конфиг инструмента и способ обхода: остальное payload знает сам."""

    model_config = ConfigDict(extra="forbid")

    OP: ClassVar[str] = "confluence_ingest"

    op: str = Field(min_length=1)
    config: dict[str, Any]
    mode: str = Field(min_length=1)
    page_ids: tuple[str, ...] = ()
    cql: str = ""
    space_keys: tuple[str, ...] = ()
    prune_missing: bool
    force_update: bool


class IngestAnswer(BaseModel):
    """Статистика прогона: collection/indexed/skipped_unchanged/pruned/failed."""

    model_config = ConfigDict(extra="forbid")

    stats: dict[str, Any]


class ConfluenceIngestCaller:
    """Один запуск payload'а на прогон: модель эмбеддера грузится однажды."""

    def __init__(
        self,
        tool: str,
        sandbox: SandboxEntryConfig,
        path_vars: Callable[[], Mapping[str, str]],
    ) -> None:
        self._entry = sandbox.entry
        self._caller = SandboxCaller(tool, sandbox.effective(), path_vars)

    def ingest(  # noqa: PLR0913 — режимы обхода независимы
        self,
        *,
        config: Mapping[str, Any],
        mode: str,
        prune_missing: bool,
        force_update: bool,
        page_ids: Sequence[str] = (),
        cql: str = "",
        space_keys: Sequence[str] = (),
    ) -> dict[str, Any]:
        request = IngestRequest(
            op=IngestRequest.OP,
            config=dict(config),
            mode=mode,
            page_ids=tuple(page_ids),
            cql=cql,
            space_keys=tuple(space_keys),
            prune_missing=prune_missing,
            force_update=force_update,
        )
        return self._caller.call_json(self._entry, request, IngestAnswer).stats
