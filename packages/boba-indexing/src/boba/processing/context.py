"""Контекст одного запуска processing-pipeline.

Имена `IndexingContext` и `PipelineId` сохранены ради совместимости с большим
количеством внешних импортёров; смысл шире индексации (любой streaming-flow
с финальной стадией Store/Sink/Collector).
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.patterns import StrId

__all__ = ["IndexingContext", "PipelineId"]


class PipelineId(StrId):
    """Идентификатор именованного pipeline'а из конфига."""


@dataclass(frozen=True)
class IndexingContext:
    """
    Контекст пробрасываемый через все стадии Source→Reader→Chunker→Store.
    """

    pipeline_id: PipelineId
    collection: str
