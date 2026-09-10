"""Наблюдение за прогоном индексации: счётчики прогресса и обёртки стадий.

Pipeline эмитит события по завершении source'а, поэтому долгие стадии внутри
него (скачивание вложения, OCR, эмбеддинг, запись в postgres) выглядят как
зависание. Обёртки здесь пишут строку на входе в каждую операцию ввода-вывода
и на выходе из неё, не собирая поток: и Reader, и Chunker остаются ленивыми
генераторами.

IngestProgress ведёт счёт по единицам прогона — space'ы, страницы, вложения,
чанки — и печатает сводку на каждое завершённое событие. Знаменатель растёт по
мере обхода: сколько всего страниц в space, Confluence заранее не сообщает,
поэтому незакрытое обнаружение помечается «+».

Логов в boba-indexing/boba-text нет намеренно, поэтому наблюдение живёт
здесь, в слое инструмента.

Ошибки: своих не выпускает; ошибки обёрнутых стадий уходят наверх как есть.
"""

from __future__ import annotations

import logging
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Generator,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Generic, TypeVar

from boba.indexing import (
    AsyncBinaryStream,
    Chunk,
    Chunker,
    ChunkerId,
    ChunkId,
    ChunkStore,
    ChunkSummary,
    CollectionId,
    CompletedItem,
    ContentHash,
    EmbeddedChunk,
    Filter,
    HashDiff,
    IndexEvent,
    IndexStats,
    IndexStatsBuilder,
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    RunFinished,
    Section,
    Severity,
    SourceFailed,
    SourceGone,
    SourceId,
    SourceIndexed,
    SourceKind,
    SourceLedger,
    SourceRecord,
    SourceSkippedUnchanged,
    TransportKeys,
)
from boba.indexing.ports import Embedder
from boba.toolkit.timing import Elapsed

__all__ = [
    "IngestProgress",
    "LoggedIndexRun",
    "LoggingChunkStore",
    "LoggingChunker",
    "LoggingEmbedder",
    "LoggingReader",
    "LoggingSourceLedger",
    "RunOutcome",
]

T = TypeVar("T")


class DbOp(StrEnum):
    """Операции хранилища чанков: каждая — поход в postgres."""

    GET_BY_IDS = "get_by_ids"
    PEEK = "peek"
    FIND = "find"
    DIFF_BY_HASH = "diff_by_hash"
    UPSERT = "upsert"
    UPDATE_METADATA = "update_metadata"
    DELETE = "delete"
    DELETE_BY_SOURCE = "delete_by_source"


class LedgerOp(StrEnum):
    """Операции реестра источников: каждая — поход в postgres."""

    LOOKUP = "lookup"
    TOUCH = "touch"
    RECORD = "record"
    UNSEEN = "unseen"
    CHILDREN = "children"
    FORGET = "forget"


@dataclass
class DiscoveredCount:
    """Сделано из обнаруженного; пока обнаружение идёт, знаменатель с «+»."""

    done: int = 0
    found: int = 0
    closed: bool = False

    def add_found(self, count: int) -> None:
        self.found += count

    def close(self) -> None:
        """Обнаружение закончено: знаменатель больше не вырастет."""
        self.closed = True

    def complete(self) -> None:
        self.done += 1

    def render(self) -> str:
        if self.closed:
            return f"{self.done}/{self.found}"

        return f"{self.done}/{self.found}+"


class IngestProgress:
    """Счёт прогона: сколько space'ов, страниц, вложений и чанков уже сделано.

    Живёт в event loop прогона — страницы идут задачами одного loop'а, поэтому
    счётчики обновляются без блокировок.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._spaces = DiscoveredCount()
        self._pages = DiscoveredCount()
        self._attachments = DiscoveredCount()
        self._chunks = 0
        self._failed = 0
        self._gone = 0
        self._skipped: dict[str, int] = {}

    def spaces_found(self, count: int) -> None:
        """Список space'ов известен целиком до обхода."""
        self._spaces.add_found(count)
        self._spaces.close()

    def space_done(self, space_key: str) -> None:
        self._spaces.complete()
        self._logger.info("space done: %s", space_key)
        self.say()

    def pages_found(self, count: int) -> None:
        self._pages.add_found(count)

    def pages_closed(self) -> None:
        """Обход discovery дошёл до конца: число страниц окончательное."""
        self._pages.close()
        self.say()

    def page_done(self) -> None:
        self._pages.complete()

    def page_failed(self) -> None:
        self._pages.complete()
        self._failed += 1

    def attachments_found(self, count: int) -> None:
        self._attachments.add_found(count)

    def attachment_skipped(self, reason: str) -> None:
        """Вложение отсечено правилами: в индекс не пойдёт, но существует."""
        self._skipped[reason] = self._skipped.get(reason, 0) + 1

    def skipped_attachments(self) -> Mapping[str, int]:
        return dict(self._skipped)

    def found_pages(self) -> int:
        return self._pages.found

    def found_attachments(self) -> int:
        return self._attachments.found

    def attachment_done(self) -> None:
        self._attachments.complete()

    def attachment_failed(self) -> None:
        self._attachments.complete()
        self._failed += 1

    def chunks_made(self, count: int) -> None:
        self._chunks += count

    def source_gone(self) -> None:
        self._gone += 1

    def render(self) -> str:
        return (
            f"progress: spaces {self._spaces.render()}"
            f" | pages {self._pages.render()}"
            f" | attachments {self._attachments.render()}"
            f" | chunks {self._chunks}"
            f" | failed {self._failed}"
            f" | gone {self._gone}"
            f" | skipped {sum(self._skipped.values())}"
        )

    def say(self) -> None:
        self._logger.info("%s", self.render())


@dataclass
class RunOutcome:
    """Итог прогона для отчёта вызывающему: счёт по видам и причины отказов.

    Причина хранится первой на вид: в отчёт инструмента уходит именно она,
    иначе вызывающий видит только число failed и не знает, что чинить.
    """

    stats: IndexStats
    reasons: dict[SourceKind, str] = field(default_factory=dict)

    def reason_of(self, kind: SourceKind) -> str:
        return self.reasons.get(kind, "")


class LoggedIndexRun:
    """Слив IndexEvent-потока с per-event логированием; возвращает RunOutcome."""

    _LEVELS: ClassVar[dict[Severity, int]] = {
        Severity.INFO: logging.INFO,
        Severity.WARN: logging.WARNING,
        Severity.ERROR: logging.ERROR,
    }

    @staticmethod
    async def drain(
        events: AsyncIterable[IndexEvent],
        logger: logging.Logger,
        progress: IngestProgress,
    ) -> RunOutcome:
        """Потребить поток Pipeline.index(...), пишет каждое событие в logger."""
        outcome = RunOutcome(stats=IndexStatsBuilder().build())
        async for event in events:
            LoggedIndexRun._emit(logger, event)
            LoggedIndexRun._count(progress, event)
            LoggedIndexRun._remember(outcome, event)
            if isinstance(event, RunFinished):
                outcome.stats = event.stats

        return outcome

    @staticmethod
    def _emit(logger: logging.Logger, event: IndexEvent) -> None:
        """Одна log-строка на событие: headline() для item'ов, label() для фаз."""
        message = (
            event.headline() if isinstance(event, CompletedItem) else event.label()
        )
        logger.log(LoggedIndexRun._LEVELS[event.severity()], "%s", message)

    @staticmethod
    def _remember(outcome: RunOutcome, event: IndexEvent) -> None:
        if not isinstance(event, SourceFailed):
            return

        if event.kind in outcome.reasons:
            return

        outcome.reasons[event.kind] = event.reason

    @staticmethod
    def _count(progress: IngestProgress, event: IndexEvent) -> None:
        """Источник закрыт своим событием; страница и вложение считаются врозь."""
        if isinstance(event, SourceGone):
            progress.source_gone()
            progress.say()
            return

        if isinstance(event, SourceFailed):
            if event.kind is SourceKind.CHILD:
                progress.attachment_failed()
            else:
                progress.page_failed()

            progress.say()
            return

        if not isinstance(event, (SourceIndexed, SourceSkippedUnchanged)):
            return

        if event.kind is SourceKind.CHILD:
            progress.attachment_done()
        else:
            progress.page_done()

        progress.say()


class LoggingChunkStore(ChunkStore[T], Generic[T]):
    """Обёртка ChunkStore: каждый поход в postgres виден до и после."""

    def __init__(self, inner: ChunkStore[T], logger: logging.Logger) -> None:
        self._inner = inner
        self._logger = logger

    @contextmanager
    def _step(
        self, op: DbOp, collection: CollectionId, count: int
    ) -> Generator[None, None, None]:
        self._logger.info("db %s start: %d in %s", op.value, count, collection)
        elapsed = Elapsed()

        yield

        self._logger.info(
            "db %s done: %d in %s in %dms",
            op.value,
            count,
            collection,
            elapsed.ms(),
        )

    async def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Sequence[Chunk[T]]:
        ids = list(chunk_ids)
        with self._step(DbOp.GET_BY_IDS, collection, len(ids)):
            return await self._inner.get_by_ids(collection, ids)

    async def peek(
        self,
        collection: CollectionId,
        *,
        source_id: SourceId | None,
        limit: int,
    ) -> Sequence[ChunkSummary[T]]:
        with self._step(DbOp.PEEK, collection, limit):
            return await self._inner.peek(collection, source_id=source_id, limit=limit)

    async def find(
        self,
        collection: CollectionId,
        *,
        where: Filter | None,
        limit: int | None = None,
    ) -> Sequence[ChunkSummary[T]]:
        self._logger.info("db find start: %s", collection)
        elapsed = Elapsed()
        found = await self._inner.find(collection, where=where, limit=limit)
        self._logger.info(
            "db find done: %d rows in %s in %dms",
            len(found),
            collection,
            elapsed.ms(),
        )

        return found

    async def diff_by_hash(
        self,
        collection: CollectionId,
        candidates: Iterable[tuple[ChunkId, ContentHash]],
    ) -> HashDiff:
        pairs = list(candidates)
        self._logger.info("db diff_by_hash start: %d in %s", len(pairs), collection)
        elapsed = Elapsed()
        diff = await self._inner.diff_by_hash(collection, pairs)
        self._logger.info(
            "db diff_by_hash done: %d to upsert, %d unchanged in %dms",
            len(diff.to_upsert),
            len(diff.unchanged),
            elapsed.ms(),
        )

        return diff

    async def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[EmbeddedChunk[T]],
    ) -> None:
        batch = list(chunks)
        with self._step(DbOp.UPSERT, collection, len(batch)):
            await self._inner.upsert(collection, batch)

        # поимённо: по этой строке видно, что конкретный чанк уже в базе.
        # source_id не печатаем — это длинный url, он уже был в строках read
        for chunk in batch:
            self._logger.info("db upserted: %s", chunk.chunk_id)

    async def update_metadata(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
        patch: Mapping[str, str | int | float | bool],
    ) -> None:
        ids = list(chunk_ids)
        with self._step(DbOp.UPDATE_METADATA, collection, len(ids)):
            await self._inner.update_metadata(collection, ids, patch)

    async def delete_by_source(
        self,
        collection: CollectionId,
        source_id: SourceId,
        *,
        from_index: int,
    ) -> int:
        with self._step(DbOp.DELETE_BY_SOURCE, collection, from_index):
            return await self._inner.delete_by_source(
                collection, source_id, from_index=from_index
            )

    async def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        ids = list(chunk_ids)
        with self._step(DbOp.DELETE, collection, len(ids)):
            await self._inner.delete(collection, ids)


class LoggingSourceLedger(SourceLedger):
    """Обёртка реестра: каждый поход в postgres виден до и после."""

    def __init__(self, inner: SourceLedger, logger: logging.Logger) -> None:
        self._inner = inner
        self._logger = logger

    @contextmanager
    def _step(self, op: LedgerOp, what: str) -> Generator[None, None, None]:
        self._logger.info("ledger %s start: %s", op.value, what)
        elapsed = Elapsed()

        yield

        self._logger.info("ledger %s done: %s in %dms", op.value, what, elapsed.ms())

    async def lookup(self, source_id: SourceId) -> SourceRecord | None:
        with self._step(LedgerOp.LOOKUP, source_id):
            return await self._inner.lookup(source_id)

    async def touch(self, source_ids: Sequence[SourceId], *, at: float) -> None:
        with self._step(LedgerOp.TOUCH, f"{len(source_ids)} sources"):
            await self._inner.touch(source_ids, at=at)

    async def record(self, record: SourceRecord) -> None:
        with self._step(LedgerOp.RECORD, record.source_id):
            await self._inner.record(record)

    async def unseen(self, *, before: float) -> AsyncIterator[SourceRecord]:
        self._logger.info("ledger %s start", LedgerOp.UNSEEN.value)
        count = 0
        async for record in self._inner.unseen(before=before):
            count += 1
            yield record

        self._logger.info("ledger %s done: %d sources", LedgerOp.UNSEEN.value, count)

    async def children(self, parent: SourceId) -> AsyncIterator[SourceRecord]:
        with self._step(LedgerOp.CHILDREN, parent):
            async for record in self._inner.children(parent):
                yield record

    async def forget(self, source_id: SourceId) -> None:
        with self._step(LedgerOp.FORGET, source_id):
            await self._inner.forget(source_id)


class LoggingStream(AsyncBinaryStream):
    """Обёртка тела документа: скачивание видно отдельно от разбора.

    Transport отдаёт RawDocument, когда пришли заголовки; сами байты тянет уже
    Reader вызовом read(). Без этой пары строк долгая закачка выглядит как
    зависший разбор.
    """

    def __init__(
        self,
        inner: AsyncBinaryStream,
        logger: logging.Logger,
        label: str,
    ) -> None:
        self._inner = inner
        self._logger = logger
        self._label = label

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._inner.__aiter__()

    async def read(self) -> bytes:
        self._logger.info("body read start: %s", self._label)
        elapsed = Elapsed()
        data = await self._inner.read()
        self._logger.info(
            "body read done: %s, %d bytes in %dms",
            self._label,
            len(data),
            elapsed.ms(),
        )

        return data


class LoggingReader(Reader[T], Generic[T]):
    """Обёртка Reader'а: пишет, что за документ пошёл в разбор и чем кончился."""

    def __init__(self, inner: Reader[T], logger: logging.Logger) -> None:
        self._inner = inner
        self._logger = logger

    def reader_id(self) -> ReaderId:
        return self._inner.reader_id()

    async def read(self, value: RawDocument) -> AsyncIterator[Section[T]]:
        title = value.metadata.get(ReaderKeys.PAGE_TITLE) or ""
        content_type = value.metadata.get(TransportKeys.CONTENT_TYPE) or "?"
        self._logger.info(
            "read start: %s [%s] reader=%s src=%s",
            title,
            content_type,
            self._inner.reader_id(),
            value.source_id,
        )
        elapsed = Elapsed()
        sections = 0
        async for section in self._inner.read(value):
            sections += 1
            yield section

        self._logger.info(
            "read done: %s -> %d sections in %dms",
            title or "?",
            sections,
            elapsed.ms(),
        )


class LoggingEmbedder(Embedder[T], Generic[T]):
    """Обёртка Embedder'а: батч эмбеддится минуты, и это самая тихая стадия."""

    def __init__(self, inner: Embedder[T], logger: logging.Logger) -> None:
        self._inner = inner
        self._logger = logger

    async def embed_documents(
        self,
        contents: Sequence[T],
    ) -> Sequence[Sequence[float]]:
        self._logger.info("embedding start: %d chunks", len(contents))
        elapsed = Elapsed()
        vectors = await self._inner.embed_documents(contents)
        self._logger.info(
            "embedding done: %d chunks in %dms", len(vectors), elapsed.ms()
        )

        return vectors

    async def embed_query(self, content: T) -> Sequence[float]:
        self._logger.info("embedding start: query")
        elapsed = Elapsed()
        vector = await self._inner.embed_query(content)
        self._logger.info("embedding done: query in %dms", elapsed.ms())

        return vector

    def dim(self) -> int:
        return self._inner.dim()


class LoggingChunker(Chunker[T], Generic[T]):
    """Обёртка Chunker'а: тик каждые EVERY чанков, чтобы прогресс был виден."""

    EVERY: ClassVar[int] = 25

    def __init__(
        self,
        inner: Chunker[T],
        logger: logging.Logger,
        progress: IngestProgress,
    ) -> None:
        self._inner = inner
        self._logger = logger
        self._progress = progress

    def chunker_id(self) -> ChunkerId:
        return self._inner.chunker_id()

    async def chunk(
        self,
        sections: AsyncIterable[Section[T]],
    ) -> AsyncIterator[Chunk[T]]:
        self._logger.info("chunking start: %s", self._inner.chunker_id())
        elapsed = Elapsed()
        produced = 0
        async for chunk in self._inner.chunk(sections):
            produced += 1
            self._progress.chunks_made(1)
            if produced % self.EVERY == 0:
                self._logger.info(
                    "chunking: %d chunks so far, src=%s",
                    produced,
                    chunk.source_id,
                )
            yield chunk

        self._logger.info("chunking done: %d chunks in %dms", produced, elapsed.ms())
