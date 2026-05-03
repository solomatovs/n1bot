"""Reader + ReaderDispatcher: SourceItem → Iterable[Section]."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence

from boba.indexing.context import IndexingContext
from boba.indexing.errors import NoMatchingReaderError
from boba.indexing.items import SourceItem
from boba.indexing.sections import Section
from boba.patterns import ContextConverter, StateFull, StreamTransformer, StrId

__all__ = ["Reader", "ReaderDispatcher", "ReaderId"]


class ReaderId(StrId):
    """Идентификатор Reader-реализации (например 'txt', 'md', 'html')."""


class Reader(
    ContextConverter[IndexingContext, SourceItem, Iterable[Section]],
    StateFull,
    ABC,
):
    """Преобразует один SourceItem в поток Section'ов.

    Reader решает, может ли он обработать item, через `accepts(item)` —
    обычно по `item.content_hint`. ReaderDispatcher делегирует первому
    подходящему. Если ни один не подошёл — NoMatchingReaderError (или
    skip_unmatched=True у диспетчера).
    """

    @abstractmethod
    def reader_id(self) -> ReaderId: ...

    @abstractmethod
    def accepts(self, item: SourceItem) -> bool: ...

    @abstractmethod
    def convert(
        self, ctx: IndexingContext, value: SourceItem
    ) -> Iterable[Section]: ...


class ReaderDispatcher(StreamTransformer[IndexingContext, SourceItem, Section]):
    """First-match dispatcher: Iterable[SourceItem] → Iterable[Section].

    Адаптер из набора Reader'ов в StreamTransformer для IndexPipeline.
    Порядок Reader'ов в `readers` задаёт приоритет матчинга.
    """

    def __init__(
        self,
        readers: Sequence[Reader],
        *,
        skip_unmatched: bool = False,
    ) -> None:
        self._readers = list(readers)
        self._skip_unmatched = skip_unmatched

    def name(self) -> str:
        return "ReaderDispatcher({})".format(
            ", ".join(r.reader_id().to_wire() for r in self._readers)
        )

    def reset(self) -> None:
        for r in self._readers:
            r.reset()

    def stream(
        self, ctx: IndexingContext, stream: Iterable[SourceItem]
    ) -> Iterable[Section]:
        for item in stream:
            reader = self._pick(item)
            if reader is None:
                if self._skip_unmatched:
                    continue
                raise NoMatchingReaderError(item.source_id, item.content_hint)
            yield from reader.convert(ctx, item)

    def _pick(self, item: SourceItem) -> Reader | None:
        return next((r for r in self._readers if r.accepts(item)), None)
