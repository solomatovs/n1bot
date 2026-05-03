"""Контекст одного запуска IndexPipeline."""

from __future__ import annotations

from dataclasses import dataclass

from boba.patterns import StrId

__all__ = ["IndexingContext", "PipelineId"]


class PipelineId(StrId):
    """Идентификатор именованного pipeline'а из конфига."""


@dataclass(frozen=True)
class IndexingContext:
    """Контекст пробрасываемый через все стадии Source→Reader→Chunker→Store.

    `collection` — логический target в Store (collection в Chroma/Qdrant,
    namespace в Pinecone, и т.п.). Стрим-стадии (Source/Reader/Chunker) его
    обычно игнорируют; Store берёт как target для upsert/delete.
    """

    pipeline_id: PipelineId
    collection: str
