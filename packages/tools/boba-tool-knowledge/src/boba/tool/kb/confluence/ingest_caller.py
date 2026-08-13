"""Вызов узла индексации: прогон целиком идёт в песочнице."""

from __future__ import annotations

from typing import Any

from pydantic import JsonValue

from boba.tool.kb.confluence.ingest_protocol import IngestAnswer, IngestSource
from boba.tool.kb.confluence.protocol import ConfluenceNode
from boba.toolkit.launcher import LauncherFactory, StageRun

__all__ = ["ConfluenceIngestCaller"]


class ConfluenceIngestCaller:
    """Один запуск узла на прогон: модель эмбеддера грузится однажды."""

    def __init__(self, tool: str, launchers: LauncherFactory) -> None:
        self._run = StageRun(launchers(tool))

    def ingest(  # noqa: PLR0913 — настройки прогона и парсера независимы
        self,
        *,
        source: IngestSource,
        prune_missing: bool,
        force_update: bool,
        ocr_enabled: bool,
        num_workers: int,
        ocr_language: str,
    ) -> dict[str, Any]:
        """Итог прогона живёт в квитанции: потока данных у узла нет."""
        args: dict[str, JsonValue] = {
            "source": source.model_dump(mode="json"),
            "prune_missing": prune_missing,
            "force_update": force_update,
            "ocr_enabled": ocr_enabled,
            "num_workers": num_workers,
            "ocr_language": ocr_language,
        }

        answer = self._run.trailer(ConfluenceNode.INGEST.value, args, IngestAnswer)

        return answer.stats
