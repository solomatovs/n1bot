# ruff: noqa: E501 — module docstring с широкими ASCII-таблицами
"""
Index views — два business-layer ABC поверх VectorStore.

Разделены по типу входных данных:

- `IndexQuery[T]` — filter-based операции (find / clean / narrow).
    Принимает Filter на вход.
    Подходит для search-сервисов, cleanup-стратегий, audit-инструментов.

Каждая реализация автоматически инжектит scope-фильтр в каждый запрос —
caller физически не может задеть данные другого scope'а.
В роли scope-key может выступать ЛЮБОЕ поле.
Реализация сама решает как фильтровать данные что бы соблюдать рамки реализуемого scope фильтра
Вот некоторые предполагаемые примеры реализаций:
    - NamespacedView(store, collection, namespace)
        фильтрует по полю `namespace` - бизнес-уровневая группировка
        нескольких `source_id`
    - TaggedView(store, collection, tag) — фильтрует по тэгам `HasTag(tag)`
    - TenantView(...) — multi-tenant изоляция по `tenant_id`
    - TimeBucketedView(...) — sharding по time-bucket
    - GlobalView(store, collection) — без scope-фильтра


- `IndexSink[T]` — data-input операция (только reconcile).
    Принимает chunks на вход — единственная операция, которой filter недостаточен
    (запись требует данных). Чанки сами несут свою идентичность через свои поля
    (chunk_id, source_id, anchor, tags), поэтому narrow на write-стороне не нужен:
    primary key уникален, sink уже задан в свой scope при конструировании.

Backend-агностично: любой impl работает поверх произвольного VectorStore


╔═══ VectorStore-схема хранения (id + payload, primary key + raw data) ════════╗
║ chunk_id    — primary key (varchar в pgvector / id в Chroma)                 ║
║ content     — Chunk.content; в Chroma → document, в pgvector → TEXT          ║
║ embedding   — vector(N), генерируется Embedder'ом на write                   ║
╚══════════════════════════════════════════════════════════════════════════════╝

╔═══ Metadata fields набор дополнительной информации сохраняемой с чанком ═════╗
║ ── Chunk-frame ───                                                           ║
║ source_id    — Chunk.source_id (логический id source-документа)              ║
║ anchor       — Chunk.anchor (heading-id и т.п.; "" если у документа якорей нет)║
║ chunk_index  — Chunk.chunk_index (порядковый номер в source)                 ║
║ loc_start    — Chunk.location.start (offset в source.content)                ║
║ loc_end      — Chunk.location.end                                            ║
║                                                                              ║
║ ── TrackingKeys ──                                                           ║
║ content_hash — Chunk.content_hash → fingerprint для idempotency              ║
║ updated_at   — refresh-timestamp; cleanup-фильтр (before cutoff)             ║
║ tags         — Chunk.tags (multi-value labels)                               ║
║                                                                              ║
║ ── Scope-keys ────.                                                          ║
║ namespace*   — пример: NamespacedView пишет namespace="docs"                 ║
║ tenant_id*   — пример: TenantView                                            ║
║                                                                              ║
║ ── Произвольные business-Metadata ─                                          ║
║ transport.etag, reader.doc_type, chunker.chunk_summary, ...                  ║
╚══════════════════════════════════════════════════════════════════════════════╝

Реляционный пример хранения чанков в таблице:
┌──────────┬────────┬──────────────┬───────────┬───────┬────────┬──────────┬─────────┬───────────────┬─────────────┬───────┬────────────┐
│ chunk_id │content │  embedding   │ source_id │anchor │ ch_idx │ loc_start│ loc_end │ content_hash  │ updated_at  │ tags  │ namespace* │
├──────────┼────────┼──────────────┼───────────┼───────┼────────┼──────────┼─────────┼───────────────┼─────────────┼───────┼────────────┤
│ abc123:0 │"Para…" │ [0.12,0.04…] │ src/p-1   │§1     │   0    │    0     │   120   │ sha256:aaa..  │ 1700000001  │ {pub} │ docs       │
│ abc123:1 │"More…" │ [0.08,0.41…] │ src/p-1   │§1     │   1    │   120    │   240   │ sha256:bbb..  │ 1700000001  │ {pub} │ docs       │
│ def456:0 │"Other" │ [0.31,0.27…] │ src/p-2   │""     │   0    │    0     │    85   │ sha256:ccc..  │ 1700000005  │ {pdf} │ docs       │
│ xyz789:0 │"Code…" │ [0.55,0.13…] │ repo/foo  │""     │   0    │    0     │   200   │ sha256:ddd..  │ 1700000010  │ {ai}  │ code       │
└──────────┴────────┴──────────────┴───────────┴───────┴────────┴──────────┴─────────┴───────────────┴─────────────┴───────┴────────────┘
(embedding-вектор обрезан до 2 элементов для отображения; реально size=N)

Слева направо:
  Store-payload:  chunk_id, content, embedding (vectorstore primary + raw)
  Chunk-frame:    source_id, anchor, ch_idx, loc_start, loc_end (поля Chunk)
  Tracking:       content_hash, updated_at, tags (управляются view-импл'ом)
  Scope-key*:     namespace* (impl-specific; пример NamespacedView)
  (Произвольная business-Metadata — Chunk.metadata.to_wire() — также
   живёт здесь как dotted-keys: `transport.etag`, `reader.doc_type` и т.п.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from boba.indexing.chunks import Chunk, ChunkSummary
from boba.indexing.filter import Filter

__all__ = [
    "IndexQuery",
    "IndexSink",
    "ReconcileSummary",
    "TrackingKeys",
]

T = TypeVar("T")


class TrackingKeys:
    """
    Универсальные metadata-ключи view-операций

    Используются всеми реализациями для хранения business-данных
    """

    CONTENT_HASH: ClassVar[str] = "content_hash"
    UPDATED_AT: ClassVar[str] = "updated_at"
    SOURCE_ID: ClassVar[str] = "source_id"
    TAGS: ClassVar[str] = "tags"


@dataclass(frozen=True)
class ReconcileSummary:
    """
    Результат `IndexSink.reconcile`

    `total`     — сколько чанков всего пришло на reconcile
    `upserted`  — новые или изменившиеся (re-embed + write в Store)
    `unchanged` — chunk_id уже был с тем же content_hash;
                  Store re-embed НЕ делает, только refresh `updated_at`
                  через `VectorStoreWriter.update_metadata(...)`
    """

    total: int
    upserted: int
    unchanged: int


class IndexQuery(ABC, Generic[T]):
    """
    Filter-based view: чтение и удаление по предикату.

    Все три метода принимают `Filter` (или его композицию)
    Реализация автоматически добавляет свой scope-фильтр
    к каждому запросу, поэтому caller физически не может затронуть данные
    другого scope'а.
    """

    @abstractmethod
    def find(
        self,
        *,
        where: Filter | None = None,
        limit: int | None = None,
    ) -> Iterable[ChunkSummary[T]]:
        """
        Scope-aware поиск по фильтру

        Реализация автоматически добавляет свой scope-фильтр к where
        какое поле выступает scope-key, решает реализация, например:
            NamespacedView — по `namespace`
            TaggedView — через `HasTag`, и т.д.

        если:
        `where=None` — только scope-фильтр.
        `limit=None` — без лимита (caller отвечает за риски).
        """
        ...

    @abstractmethod
    def clean(self, where: Filter) -> int:
        """
        Удалить chunk в текущем scope, удовлетворяющие фильтру.

        Возвращает количество удалённых. реализация автоматически добавляет свой
        scope-фильтр к `where`.
        `where` обязательный — пустого фильтра в API нет
        (предохранитель от случайной полной зачистки scope)

        Используется CleanupStrategy для wipe stale-записей:
            clean(where=And([
                Lt(TrackingKeys.UPDATED_AT, run_start),
                In(TrackingKeys.SOURCE_ID, [s.to_wire() for s in touched]),
            ]))

        Backend может оптимизировать в одно DELETE WHERE
        """
        ...

    @abstractmethod
    def narrow(self, where: Filter) -> IndexQuery[T]:
        """
        Делаем новый IndexQuery с добавлением указанного Filter
        По сути комбинирует уже существующий фильтр с новым указанным

        Полезно для suscope-поиска:
            view.narrow(HasTag("public")).find(where=...)
            view.narrow(Eq(TrackingKeys.SOURCE_ID, "src/X")).clean(
                where=Lt(TrackingKeys.UPDATED_AT, t)
            )

        narrow можно вызывать каскадом:
            `view.narrow(a).narrow(b)` ≡ `view.narrow(And([a, b]))`
        """
        ...


class IndexSink(ABC, Generic[T]):
    """
    Запись chunk'ов через reconcile

    `chunks`: Iterable[Chunk[T]] - делает идемпотентную проверку
        если chunk не изменился, то обновление content не происходит (только метаданные)
        если chunk изменился, то обновление всего chunk
    """

    @abstractmethod
    def reconcile(
        self,
        chunks: Iterable[Chunk[T]],
        *,
        time_at_least: float,
        force: bool = False,
    ) -> ReconcileSummary:
        """
        Атомарно «привести в соответствие» чанки

        Все чанки в одном вызове должны логически принадлежать одной партии
        (обычно — одному source'у). Идентичность каждого чанка несут его
        собственные поля (chunk_id, source_id, anchor, tags, …); sink сам
        инжектит свой scope-tag (например `namespace="docs"`) при write,
        caller не задаёт scope извне.

        Контракт:
        - `chunks` приходят с уже посчитанным `Chunk.content_hash`
          (caller заранее прогнал `KeyEncoder.encode`)
        - Для каждого chunk_id идемпотентная проверка:
          chunk_id уже в учёте + content_hash совпадает → unchanged
        - Изменившиеся / новые → `vector_store.upsert(...)` (re-embed)
        - Все чанки (incl. unchanged) → `vector_store.update_metadata(...)`
          для refresh `updated_at`, чтобы cleanup не считал их stale
        - `force=True` пропускает idempotency-check и трактует все как dirty

        Возвращает `ReconcileSummary(total, upserted, unchanged)`.

        narrow на write-стороне НЕ нужен: чанки сами несут свою идентичность,
        primary key (chunk_id) уникален в Store, scope sink'а задан при его
        конструировании. Если нужен scope-narrowed query — это IndexQuery.narrow.
        """
        ...
