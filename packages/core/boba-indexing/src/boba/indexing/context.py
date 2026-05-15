"""
Контекст одного запуска pipeline'а
который пробрасывается через все стадии от Source до Store
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

__all__ = ["CollectionId", "NamespaceId", "PipelineContext", "PipelineId"]


PipelineId = NewType("PipelineId", str)
"""Идентификатор именованного pipeline'а из конфига."""


CollectionId = NewType("CollectionId", str)
"""Идентификатор коллекции в векторной базе (Chroma/Qdrant collection).

Бэкэнд-уровневый scope: всё, что лежит в одной collection, физически
хранится вместе. На один backend — много коллекций.
"""


NamespaceId = NewType("NamespaceId", str)
"""Логический scope для view-учёта внутри одной коллекции.

Namespace — business-уровневая изоляция: несколько view-импл'ов
(IndexQuery+IndexSink) могут работать на одной collection, но видеть
только записи своего namespace. Реализуется как scope-фильтр на
каждом query/write — конкретное поле (namespace, tag, tenant_id, ...)
выбирает impl.
"""


@dataclass(frozen=True)
class PipelineContext:
    """
    Контекст пробрасываемый через все стадии Source→Reader→Chunker.

    Сейчас держит только `pipeline_id` для observability/логирования.
    Collection и namespace — атрибуты view-импл'а (бизнес-уровень),
    pipeline их не знает.
    """

    pipeline_id: PipelineId
