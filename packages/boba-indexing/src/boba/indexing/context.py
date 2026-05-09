"""
Контекст одного запуска pipeline'а
который пробрасывается через все стадии от Source до Store
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.patterns import StrId

__all__ = ["CollectionId", "NamespaceId", "PipelineContext", "PipelineId"]


class PipelineId(StrId):
    """Идентификатор именованного pipeline'а из конфига."""


class CollectionId(StrId):
    """Идентификатор коллекции в векторной базе (collection в Chroma/Qdrant и т.п.)."""


class NamespaceId(StrId):
    """Логический scope для RecordManager-учёта (изоляция bucket'а между pipeline'ами)."""


@dataclass(frozen=True)
class PipelineContext:
    """
    Контекст пробрасываемый через все стадии Source→Reader→Chunker→Store.
    """

    pipeline_id: PipelineId
    collection: CollectionId
