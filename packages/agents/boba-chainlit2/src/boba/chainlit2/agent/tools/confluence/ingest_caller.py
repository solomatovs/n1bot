"""Вызов ingest-payload'а: индексация целиком идёт в песочнице."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr

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

    @classmethod
    def config_of(cls, cfg: BaseModel) -> dict[str, Any]:
        """Секреты раскрываются здесь: SecretStr не сериализуется сам собой."""
        return cls._reveal(cfg.model_dump(mode="json"), cfg.model_dump())

    @classmethod
    def _reveal(cls, jsonable: Any, native: Any) -> Any:
        if isinstance(native, SecretStr):
            return native.get_secret_value()
        if isinstance(native, dict):
            revealed: dict[str, Any] = {}
            for key, value in native.items():
                revealed[key] = cls._reveal(jsonable[key], value)
            return revealed
        if isinstance(native, (list, tuple)):
            items: list[Any] = []
            for index, value in enumerate(native):
                items.append(cls._reveal(jsonable[index], value))
            return items
        return jsonable

    def ingest(  # noqa: PLR0913 — режимы обхода независимы
        self,
        *,
        cfg: BaseModel,
        mode: str,
        prune_missing: bool,
        force_update: bool,
        page_ids: Sequence[str] = (),
        cql: str = "",
        space_keys: Sequence[str] = (),
    ) -> dict[str, Any]:
        request = IngestRequest(
            op=IngestRequest.OP,
            config=self.config_of(cfg),
            mode=mode,
            page_ids=tuple(page_ids),
            cql=cql,
            space_keys=tuple(space_keys),
            prune_missing=prune_missing,
            force_update=force_update,
        )
        return self._caller.call_json(self._entry, request, IngestAnswer).stats
