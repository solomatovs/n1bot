"""Source: StreamSource[IndexingContext, SourceItem] + identity + Factory."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from boba.indexing.context import IndexingContext
from boba.indexing.extension import IndexerExtensionContext
from boba.indexing.items import SourceItem
from boba.patterns import ContextItemProvider, StreamSource, StrId

__all__ = ["Source", "SourceFactory", "SourceId"]


class SourceId(StrId):
    """Идентификатор Source-реализации (например 'fs', 'confluence_space')."""


class Source(StreamSource[IndexingContext, SourceItem], ABC):
    """Источник документов для индексации.

    Реализация — StreamSource из boba-patterns. `name()` обязателен (через
    `StateLess`), `reset()` — опционален. Stream должен быть re-iterable
    в рамках одного экземпляра между `reset()`-ами (для retry-логики).

    Дополнительно знает свой `SourceId` — для discovery и сообщений об
    ошибках. Метод `list_source_ids()` опциональный — нужен только тем
    Source'ам, которые поддерживают `sync` (удаление осиротевших чанков).
    """

    @abstractmethod
    def source_factory_id(self) -> SourceId: ...

    def list_source_ids(self) -> Iterable[str] | None:
        """Все source_id, которые этот Source может произвести.

        None — Source не поддерживает sync (например стрим-источник без
        конечного перечисления).
        """
        return None


class SourceFactory(
    ContextItemProvider[IndexerExtensionContext, SourceId, Source],
    ABC,
):
    """Фабрика Source: AppConfig → готовый параметризованный Source.

    Реализация читает свою ConfigSection через `ctx.config.section(...)`
    и собирает Source с применёнными параметрами. Регистрируется в
    `SourceRegistry` через entry-point `boba.indexing.sources`.
    """

    @abstractmethod
    def id(self) -> SourceId: ...

    @abstractmethod
    def produce(self, ctx: IndexerExtensionContext) -> Source: ...
