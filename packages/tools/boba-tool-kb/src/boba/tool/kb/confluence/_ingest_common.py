"""Общая сборка Confluence-ingest pipeline для tools `confluence_ingest_*`.

Все три режима (`spaces` / `pages` / `cql`) делают одно и то же:
берут `RequestSource` (по выбранному режиму), гонят через
`ConfluenceContentTransport` (HTTP + JSON-decode + attachment fan-out) →
`DispatchReader` по `CONTENT_TYPE` → `StructuralChunker` →
`CollectionScopedView` → `PostgresChunkStore`.

`DispatchReader.on_unknown="skip"` — поток смешанный (HTML-страницы +
произвольные attachment'ы); индексируем только то, для чего знаем Reader.
PDF/картинки/прочие бинари молча пропускаются — это not-an-error.

`run_confluence_ingest` — generator-helper: yield-ит `ToolProgressReported`
per `IndexEvent` от `StreamingIndexer.stream(...)`, и через `return`
отдаёт финальный stats-dict. Контракт инфраструктуры:
**yield = progress, return = result**. `DishkaTool` ловит StopIteration.value
и заворачивает его в `ToolStreamCompleted`.

Внешние tool'ы (`confluence_ingest_spaces`/`_pages`/`_cql`) собирают
результат через `stats = yield from run_confluence_ingest(...)` —
стандартная Python-композиция, прогрессы пробрасываются прозрачно,
return-значение приходит через `yield from`-expression.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

from boba.indexing import (
    CollectionScopedView,
    DispatchReader,
    FullCleanup,
    IndexerConfig,
    NoneCleanup,
    PipelineContext,
    RequestSource,
    StreamingIndexer,
    TransportKeys,
)
from boba.indexing.context import CollectionId, PipelineId
from boba.indexing.embedder import Embedder
from boba.indexing.events import (
    BatchStarted,
    BatchUpserted,
    CompletedItem,
    IndexEvent,
    PhaseTransition,
    RunFinished,
    RunStarted,
    Severity,
)
from boba.indexing.reader import ReaderId
from boba.text import StructuralChunker
from boba.tool.kb.confluence._pipeline_common import make_confluence_transport
from boba.tool.kb.confluence.attachments import AttachmentFilter
from boba.tool.kb.confluence.connection import ConfluenceConnection
from boba.tool.kb.confluence.reader import ConfluenceReader
from boba.tool.kb.core.postgres_store import (
    PostgresChunkStore,
    PostgresCollectionsStore,
)
from boba.tools.domain import (
    ToolProgressReported,
    ToolSeverity,
)
from boba.transport.http import HttpRequest

__all__ = ["run_confluence_ingest"]


_CONFLUENCE_HTML_CONTENT_TYPES = ("text/html",)
"""CONTENT_TYPE-значения, которые `ConfluenceJsonDecoder` ставит после
распаковки JSON→HTML; всё, что попадает под эти ключи, идёт в HTML-Reader."""


def run_confluence_ingest(  # noqa: PLR0913 — keyword-only helper, явный набор deps
    *,
    request_source: RequestSource[HttpRequest],
    conn: ConfluenceConnection,
    chunk_store: PostgresChunkStore,
    collections_store: PostgresCollectionsStore,
    embedder: Embedder[str],
    chunker: StructuralChunker,
    collection: str,
    prune_missing: bool,
    pipeline_id: PipelineId,
    attachment_filter: AttachmentFilter | None = None,
) -> Generator[ToolProgressReported, None, dict[str, Any]]:
    """Полный Confluence → kb_chunks pipeline; yield-ит прогресс per source.

    Generator yield-ит `ToolProgressReported` per non-terminal `IndexEvent`
    (`SourceIndexed`/`SourceSkipped*`/`SourceFailed`/`CleanupStarted`/
    `ChunksDeleted`) и через `return` отдаёт stats-dict:
    `{collection, indexed, skipped_unchanged, pruned, failed}`. Caller
    (tool-функция) собирает результат через `stats = yield from
    run_confluence_ingest(...)` и augment-ит свои discriminator-поля.

    Шумные внутренние события (RunStarted/RunFinished/Batch*) фильтруются:
    они не несут UI-смысла — старт/финиш и так очевидны по началу/концу
    stream'а, а батчи слишком granular.

    Reader — `DispatchReader` по `TransportKeys.CONTENT_TYPE`: HTML
    обрабатывается `ConfluenceReader`'ом, всё остальное (attachment'ы PDF/
    image/etc.) молча пропускается. Когда появятся Reader'ы для PDF или
    картинок, их можно подключить добавив entry в `routes`-mapping.
    """
    confluence_reader = ConfluenceReader()
    reader: DispatchReader[str] = DispatchReader(
        by=TransportKeys.CONTENT_TYPE,
        routes=dict.fromkeys(_CONFLUENCE_HTML_CONTENT_TYPES, confluence_reader),
        reader_id=ReaderId("ext.confluence_dispatch"),
        on_unknown="skip",
    )

    collection_id = CollectionId(collection)
    collections_store.ensure_collection(collection_id, description=None)

    view: CollectionScopedView[str] = CollectionScopedView(
        store=chunk_store,
        embedder=embedder,
        collection=collection_id,
    )
    indexer: StreamingIndexer[HttpRequest, str] = StreamingIndexer(
        request_source=request_source,
        transport=make_confluence_transport(
            conn, attachment_filter=attachment_filter,
        ),
        decoders=(),
        reader=reader,
        chunker=chunker,
        sink=view,
        query=view,
    )
    config: IndexerConfig[str] = IndexerConfig(
        cleanup=FullCleanup() if prune_missing else NoneCleanup(),
        force_update=False,
    )

    final_stats = None
    for event in indexer.stream(PipelineContext(pipeline_id=pipeline_id), config):
        if isinstance(event, RunFinished):
            final_stats = event.stats
            continue
        progress = _index_event_to_progress(event)
        if progress is not None:
            yield progress

    if final_stats is None:
        # StreamingIndexer всегда yield-ит RunFinished — но если контракт
        # вдруг нарушится, лучше явная ошибка, чем silent корраптный result.
        msg = "StreamingIndexer не эмитнул RunFinished — нет финальной статистики"
        raise RuntimeError(msg)

    # `return` через StopIteration.value — DishkaTool ловит и заворачивает в TSC.
    return {
        "collection": str(collection_id),
        "indexed": final_stats.chunks_upserted,
        "skipped_unchanged": final_stats.sources_skipped_unchanged,
        "pruned": final_stats.chunks_deleted,
        "failed": final_stats.sources_failed,
    }


def _index_event_to_progress(
    event: IndexEvent,
) -> ToolProgressReported | None:
    """`IndexEvent` → `ToolProgressReported` или None (skip).

    Skip-фильтр:
    - `RunStarted` — старт и так подразумевается фактом stream'а.
    - `BatchStarted` / `BatchUpserted` — слишком granular, спам в UI.
    `RunFinished` фильтруется выше caller'ом (нужен сам объект stats).
    """
    if isinstance(event, RunStarted | BatchStarted | BatchUpserted):
        return None

    if isinstance(event, CompletedItem):
        headline = event.headline()
    elif isinstance(event, PhaseTransition):
        headline = event.label()
    else:  # pragma: no cover — IndexEvent — sealed union
        return None

    return ToolProgressReported(
        headline=headline,
        details=dict(event.details()),
        severity=_map_severity(event.severity()),
    )


def _map_severity(severity: Severity) -> ToolSeverity:
    """`boba.indexing.events.Severity` → `boba.tools.domain.ToolSeverity`."""
    match severity:
        case Severity.INFO:
            return ToolSeverity.INFO
        case Severity.WARN:
            return ToolSeverity.WARN
        case Severity.ERROR:
            return ToolSeverity.ERROR
