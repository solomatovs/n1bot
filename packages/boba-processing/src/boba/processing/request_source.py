"""RequestSource: генерирует план запросов к внешней системе.

Generic по типу Request'а: `RequestSource[HttpRequest]` для REST-API,
`RequestSource[FsRequest]` для файловой системы. Pipeline и Transport
параметризуются тем же типом — type-checker не даст совместить
несовместимые слои.

source_id формирует RequestSource — он один знает связь между
URL/path'ом и логическим документом.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Generic, TypeVar

from boba.patterns import StreamSource
from boba.processing.context import IndexingContext
from boba.processing.errors import SyncUnsupportedError
from boba.processing.request import Request

__all__ = ["RequestSource"]

ReqT = TypeVar("ReqT", bound=Request)


class RequestSource(StreamSource[IndexingContext, ReqT], ABC, Generic[ReqT]):
    """Источник Request-планов для Transport'а.

    `stream(ctx)` — generator, yield'ит Request'ы лениво.

    `list_source_ids(ctx)` — для sync-операции: какие source_id'ы будут
    произведены этим RequestSource'ом без фактического fetch'а тел.
    Бросает `SyncUnsupportedError`, если перечисление невозможно.
    """

    @abstractmethod
    def list_source_ids(self, ctx: IndexingContext) -> Iterable[str]:
        """Перечислить canonical source_id'ы; SyncUnsupportedError если нет."""
        del ctx
        raise SyncUnsupportedError(self.name())
