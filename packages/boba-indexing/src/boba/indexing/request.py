"""
Request: общий Protocol для всех request-DTO.

Конкретные Request-DTO живут в transport-пакетах:
- `HttpRequest` в `boba-ext-http-transport` (url + method + headers + auth).
- `FsRequest` в `boba-ext-fs-transport` (path).

Pipeline и `RequestSource[ReqT]` параметризуются конкретным типом —
generic-параметр `ReqT` ограничен этим protocol'ом, что даёт type-safety
(HttpTransport не примет FsRequest).


RequestSource - источник Request-планов для Transport'а.

- `RequestSource[HttpRequest]` для REST-API
- `RequestSource[FsRequest]` для файловой системы.

Pipeline и Transport параметризуются тем же типом — type-checker
не даст совместить несовместимые слои.

source_id формирует RequestSource
он один знает связь между URL/path'ом и логическим документом
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Generic, Protocol, TypeVar, runtime_checkable

from boba.indexing.context import PipelineContext
from boba.indexing.errors import SyncUnsupportedError
from boba.indexing.metadata import Metadata
from boba.indexing.sections import SourceId
from boba.patterns import StreamSource

__all__ = ["Request", "RequestSource"]


@runtime_checkable
class Request(Protocol):
    """
    Минимальный контракт Request-DTO для прохождения через Pipeline.
    """

    @property
    def source_id(self) -> SourceId: ...

    """
    Canonical id итогового документа.
    """

    @property
    def metadata(self) -> Metadata: ...

    """
    Hint'ы для обогащения Chunk.metadata.
    Каждый слой добавляет свои ключи (merge), не теряя предыдущие
    """


ReqT = TypeVar("ReqT", bound=Request)


class RequestSource(StreamSource[PipelineContext, ReqT], ABC, Generic[ReqT]):
    """
    Источник Request-планов для Transport'а.
    """

    @abstractmethod
    def list_source_ids(self, ctx: PipelineContext) -> Iterable[str]:
        """
        Перечислить canonical source_id'ы; SyncUnsupportedError если нет
        """
        del ctx
        raise SyncUnsupportedError(self.name())
