"""Pipeline — сборка стадий source -> transport -> reader с двумя терминалами:
sections() и index().

Источники обходятся параллельно: в полёте не больше config.workers штук.
Стадии соединены async-потоками, поэтому ожидание сети и записи одного
источника перекрывается работой остальных. Синхронную часть (разбор документа,
инференс эмбеддера) уносят с loop'а сами реализации портов.

Источник, отсечённый правилами обхода, отмечается увиденным и даёт
SourceSkipped: он существует, просто не нужен. Остальные сверяются с
реестром: совпавший отпечаток даёт SourceSkippedUnchanged без запроса к телу.
Скачанное тело сверяется по хэшу: тот же байт в байт документ не разбирается
заново.

Очистка идёт по метке прогона, а не по времени: снимаются дети родителей,
увиденных этим прогоном, и корни его области, не подтверждённые пробой.
Поэтому одновременные прогоны по соседним областям не принимают чужие
источники за исчезнувшие.

Отказ источника изолируется: любая ошибка стадий этого источника становится
событием SourceFailed с причиной и попадает в счётчик sources_failed, остальные
источники идут дальше. Ошибка реестра (LedgerError) не изолируется — без
реестра прогон бессмыслен, она обрывает его.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from boba.indexing.errors import IndexingError, SourceGoneError
from boba.indexing.events import (
    ChunksDeleted,
    CleanupStarted,
    IndexEvent,
    IndexStats,
    IndexStatsBuilder,
    RunFinished,
    RunId,
    RunStarted,
    SourceFailed,
    SourceGone,
    SourceIndexed,
    SourceKind,
    SourceSkipped,
    SourceSkippedUnchanged,
    new_run_id,
)
from boba.indexing.ledger import (
    ChangePolicy,
    LedgerError,
    RunScope,
    SourceLedger,
    SourceProbe,
    SourceRecord,
)
from boba.indexing.ports import Chunker, Reader, Request, RequestSource, Transport
from boba.indexing.sections import Section, SourceId
from boba.indexing.store import IndexSink
from boba.indexing.values import TransportKeys

__all__ = ["IndexerConfig", "Pipeline"]

ReqT = TypeVar("ReqT", bound=Request)
T = TypeVar("T")


@dataclass(frozen=True, kw_only=True)
class IndexerConfig(Generic[T]):
    """Параметры одного прогона Pipeline.index / Pipeline.run."""

    workers: int
    """Сколько источников обрабатывается одновременно"""

    stamp: str
    """Штамп конвейера: модель, нарезка, ридеры. Запись реестра с другим штампом
    устарела, источник индексируется заново."""

    scope: str = ""
    """Область владения обхода: в её пределах прогон снимает корни, которых
    больше нет у источника данных. Пустая область не удаляет ни одного корня —
    выборка по запросу не говорит, что не попавшее в неё исчезло."""

    def __post_init__(self) -> None:
        if self.workers < 1:
            msg = f"IndexerConfig.workers must be >= 1, got {self.workers}"
            raise ValueError(msg)


@dataclass
class _BodyTrace:
    """Что транспорт сообщил о теле по ходу одного источника."""

    body_hash: str = ""
    parsed: bool = False


class Pipeline(Generic[ReqT, T]):
    """Сборка стадий source -> transport -> reader с двумя терминалами."""

    PROBE_BATCH: ClassVar[int] = 50
    """Сколько невиденных корней уходит в SourceProbe одним пакетом."""

    def __init__(
        self,
        *,
        source: RequestSource[ReqT],
        transport: Transport[ReqT],
        reader: Reader[T],
        ledger: SourceLedger,
        probe: SourceProbe,
    ) -> None:
        self._source = source
        self._transport = transport
        self._reader = reader
        self._ledger = ledger
        self._probe = probe

    async def sections(self) -> AsyncIterator[Section[T]]:
        """Плоский поток Section[T] по всем источникам; без реестра и записи."""
        async for request in self._source.requests():
            async for raw in self._transport.fetch(request):
                async for section in self._reader.read(raw):
                    yield section

    async def index(
        self,
        *,
        chunker: Chunker[T],
        sink: IndexSink[T],
        config: IndexerConfig[T],
    ) -> AsyncIterator[IndexEvent]:
        """Индексировать все источники; ленивый поток IndexEvent, cleanup после всех."""
        run_id = new_run_id()
        scope = RunScope(run=str(run_id), scope=config.scope)
        stats = IndexStatsBuilder()

        yield RunStarted(run_id=run_id, monotonic_ns=time.monotonic_ns())

        async for event in self._index_sources(
            chunker=chunker,
            sink=sink,
            config=config,
            run_id=run_id,
            scope=scope,
        ):
            self._observe(event, stats=stats)
            yield event

        async for event in self._run_cleanup(
            sink=sink,
            run_id=run_id,
            scope=scope,
        ):
            self._observe(event, stats=stats)
            yield event

        yield RunFinished(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            stats=stats.build(),
        )

    async def run(
        self,
        *,
        chunker: Chunker[T],
        sink: IndexSink[T],
        config: IndexerConfig[T],
    ) -> IndexStats:
        """Прогнать index до конца; вернуть итоговый IndexStats."""
        async for event in self.index(chunker=chunker, sink=sink, config=config):
            if isinstance(event, RunFinished):
                return event.stats

        return IndexStatsBuilder().build()

    async def _index_sources(
        self,
        *,
        chunker: Chunker[T],
        sink: IndexSink[T],
        config: IndexerConfig[T],
        run_id: RunId,
        scope: RunScope,
    ) -> AsyncIterator[IndexEvent]:
        """Обход источников по config.workers штук в полёте; событие — как готово."""
        pending: set[asyncio.Task[Sequence[IndexEvent]]] = set()
        try:
            async for request in self._source.requests():
                pending.add(
                    asyncio.create_task(
                        self._process_source(
                            request=request,
                            chunker=chunker,
                            sink=sink,
                            config=config,
                            run_id=run_id,
                            scope=scope,
                        ),
                    ),
                )
                if len(pending) < config.workers:
                    continue

                events, pending = await self._harvest(pending)
                for event in events:
                    yield event

            while pending:
                events, pending = await self._harvest(pending)
                for event in events:
                    yield event
        finally:
            await self._drop(pending)

    @staticmethod
    async def _harvest(
        pending: set[asyncio.Task[Sequence[IndexEvent]]],
    ) -> tuple[list[IndexEvent], set[asyncio.Task[Sequence[IndexEvent]]]]:
        """Дождаться первого доработавшего источника: его события и остаток."""
        done, rest = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )

        events: list[IndexEvent] = []
        for task in done:
            events.extend(task.result())

        return events, rest

    @staticmethod
    async def _drop(pending: set[asyncio.Task[Sequence[IndexEvent]]]) -> None:
        """Снять недоработавшие источники: потребитель ушёл или упал."""
        if not pending:
            return

        for task in pending:
            task.cancel()

        await asyncio.gather(*pending, return_exceptions=True)

    async def _process_source(  # noqa: PLR0913 — стадии источника независимы
        self,
        *,
        request: ReqT,
        chunker: Chunker[T],
        sink: IndexSink[T],
        config: IndexerConfig[T],
        run_id: RunId,
        scope: RunScope,
    ) -> Sequence[IndexEvent]:
        """Один источник от сверки с реестром до записи; ровно одно итоговое событие."""
        source_id = self._transport.source_id(request)
        kind = SourceKind.of(request.mark.parent)
        now = time.time()
        try:
            if reason := request.mark.skip:
                await self._ledger.touch([source_id], at=now, scope=scope)
                skipped = SourceSkipped(
                    run_id=run_id,
                    monotonic_ns=time.monotonic_ns(),
                    source_id=source_id,
                    kind=kind,
                    reason=reason,
                )
                return [skipped]

            record = await self._ledger.lookup(source_id)
            if ChangePolicy.unchanged(record, request.mark, config.stamp):
                await self._ledger.touch([source_id], at=now, scope=scope)
                skipped = SourceSkippedUnchanged(
                    run_id=run_id,
                    monotonic_ns=time.monotonic_ns(),
                    source_id=source_id,
                    kind=kind,
                    chunks_total=0,
                )
                return [skipped]

            return await self._index_source(
                request=request,
                source_id=source_id,
                record=record,
                chunker=chunker,
                sink=sink,
                config=config,
                run_id=run_id,
                scope=scope,
                now=now,
            )
        except LedgerError:
            raise
        except SourceGoneError:
            # источник данных ответил «нет такого»: это факт, а не догадка
            return await self._forget_source(source_id, sink, run_id, kind)
        except Exception as exc:
            # запись о прошлой индексации остаётся увиденной: сорвавшийся источник
            # существует, и очистка не должна принять его за исчезнувший
            await self._ledger.touch([source_id], at=now, scope=scope)
            failed = SourceFailed(
                run_id=run_id,
                monotonic_ns=time.monotonic_ns(),
                source_id=source_id,
                kind=SourceKind.of(request.mark.parent),
                reason=Pipeline._reason(exc),
            )
            return [failed]

    async def _forget_source(
        self,
        source_id: SourceId,
        sink: IndexSink[T],
        run_id: RunId,
        kind: SourceKind,
    ) -> Sequence[IndexEvent]:
        """Источник и его дети сняты по ответу источника данных."""
        events: list[IndexEvent] = []
        async for child in self._ledger.children(source_id):
            events.append(
                await self._forget(child.source_id, sink, run_id, SourceKind.CHILD)
            )

        events.append(await self._forget(source_id, sink, run_id, kind))
        return events

    async def _index_source(  # noqa: PLR0913 — стадии одного источника независимы
        self,
        *,
        request: ReqT,
        source_id: SourceId,
        record: SourceRecord | None,
        chunker: Chunker[T],
        sink: IndexSink[T],
        config: IndexerConfig[T],
        run_id: RunId,
        scope: RunScope,
        now: float,
    ) -> Sequence[IndexEvent]:
        kind = SourceKind.of(request.mark.parent)
        trace = _BodyTrace()
        sections = self._sections_of(
            request=request,
            record=record,
            stamp=config.stamp,
            trace=trace,
        )
        summary = await sink.reconcile(chunker.chunk(sections))

        events: list[IndexEvent] = []
        if trace.parsed:
            dropped = await sink.forget(source_id, from_index=summary.total)
            if dropped:
                events.append(
                    ChunksDeleted(
                        run_id=run_id,
                        monotonic_ns=time.monotonic_ns(),
                        source_id=source_id,
                        kind=kind,
                        count=dropped,
                    )
                )

        await self._ledger.record(
            SourceRecord(
                source_id=source_id,
                parent=request.mark.parent,
                fingerprint=request.mark.fingerprint,
                content_hash=trace.body_hash,
                grade=request.mark.grade,
                stamp=config.stamp,
                seen_at=now,
                indexed_at=now,
                seen_run=scope.run,
                scope=scope.scope,
            )
        )

        if summary.upserted == 0:
            events.append(
                SourceSkippedUnchanged(
                    run_id=run_id,
                    monotonic_ns=time.monotonic_ns(),
                    source_id=source_id,
                    kind=kind,
                    chunks_total=summary.total,
                )
            )
            return events

        events.append(
            SourceIndexed(
                run_id=run_id,
                monotonic_ns=time.monotonic_ns(),
                source_id=source_id,
                kind=kind,
                chunks_total=summary.total,
                chunks_upserted=summary.upserted,
                chunks_skipped=summary.unchanged,
            )
        )
        return events

    async def _sections_of(
        self,
        *,
        request: ReqT,
        record: SourceRecord | None,
        stamp: str,
        trace: _BodyTrace,
    ) -> AsyncIterator[Section[T]]:
        """transport -> reader для одного request'а; тело с тем же хэшем не читается."""
        async for raw in self._transport.fetch(request):
            body_hash = raw.metadata.get(TransportKeys.BODY_HASH)
            if body_hash is not None:
                trace.body_hash = body_hash

            if ChangePolicy.same_body(record, request.mark, stamp, trace.body_hash):
                continue

            trace.parsed = True
            async for section in self._reader.read(raw):
                yield section

    @staticmethod
    def _reason(exc: Exception) -> str:
        """Причина отказа источника; чужой тип называет себя сам."""
        if isinstance(exc, IndexingError):
            return str(exc)

        return f"{type(exc).__name__}: {exc}"

    async def _run_cleanup(
        self,
        *,
        sink: IndexSink[T],
        run_id: RunId,
        scope: RunScope,
    ) -> AsyncIterator[IndexEvent]:
        """Снять то, чего обход не нашёл: сначала детей увиденных родителей,
        затем корни своей области, не подтверждённые пробой."""
        yield CleanupStarted(run_id=run_id, monotonic_ns=time.monotonic_ns())

        async for record in self._ledger.orphans(scope.run):
            yield await self._forget(record.source_id, sink, run_id, SourceKind.CHILD)

        if not scope.owns_roots():
            return

        roots: list[SourceRecord] = []
        async for record in self._ledger.unseen_roots(scope.scope, scope.run):
            roots.append(record)
            if len(roots) < self.PROBE_BATCH:
                continue

            async for event in self._forget_gone(roots, sink, run_id):
                yield event

            roots = []

        if roots:
            async for event in self._forget_gone(roots, sink, run_id):
                yield event

    async def _forget_gone(
        self,
        roots: Sequence[SourceRecord],
        sink: IndexSink[T],
        run_id: RunId,
    ) -> AsyncIterator[IndexEvent]:
        for source_id in await self._probe.gone(roots):
            async for child in self._ledger.children(source_id):
                yield await self._forget(
                    child.source_id, sink, run_id, SourceKind.CHILD
                )

            yield await self._forget(source_id, sink, run_id, SourceKind.ROOT)

    async def _forget(
        self,
        source_id: SourceId,
        sink: IndexSink[T],
        run_id: RunId,
        kind: SourceKind,
    ) -> IndexEvent:
        deleted = await sink.forget(source_id, from_index=0)
        await self._ledger.forget(source_id)
        return SourceGone(
            run_id=run_id,
            monotonic_ns=time.monotonic_ns(),
            source_id=source_id,
            kind=kind,
            chunks_deleted=deleted,
        )

    @staticmethod
    def _observe(event: IndexEvent, *, stats: IndexStatsBuilder) -> None:
        """Единственная точка обновления stats; один вызов на event."""
        if isinstance(event, SourceIndexed):
            tally = stats.of(event.kind)
            tally.seen += 1
            tally.indexed += 1
            tally.chunks_upserted += event.chunks_upserted

        elif isinstance(event, SourceSkippedUnchanged):
            tally = stats.of(event.kind)
            tally.seen += 1
            tally.unchanged += 1

        elif isinstance(event, SourceSkipped):
            stats.of(event.kind).skipped += 1

        elif isinstance(event, SourceFailed):
            tally = stats.of(event.kind)
            tally.seen += 1
            tally.failed += 1

        elif isinstance(event, ChunksDeleted):
            stats.of(event.kind).chunks_deleted += event.count

        elif isinstance(event, SourceGone):
            tally = stats.of(event.kind)
            tally.deleted += 1
            tally.chunks_deleted += event.chunks_deleted
