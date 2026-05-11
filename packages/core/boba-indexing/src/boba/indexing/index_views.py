# ruff: noqa: E501 — module docstring с широкими ASCII-таблицами
"""
Index views — два business-layer ABC поверх VectorStore.

- `IndexQuery[T]` — filter-based операции (`find` / `clean` / `narrow`).
  Принимает `Filter` на вход. Реализация автоматически инжектит scope-фильтр
  в каждый запрос — caller физически не может задеть данные другого scope'а.
  В роли scope-key может выступать ЛЮБОЕ поле; impl сам решает какое.
  Возможные реализации:
    - `NamespacedView(store, collection, namespace)` — scope по `namespace`
    - `TaggedView(store, collection, tag)` — scope через `HasTag(tag)`
    - `TenantView(...)` — multi-tenant изоляция по `tenant_id`
    - `TimeBucketedView(...)` — sharding по time-bucket
    - `GlobalView(store, collection)` — без scope-фильтра

- `IndexSink[T]` — data-input операция (только `reconcile`). Чанки сами несут
  свою идентичность (`chunk_id`, `source_id`, `chunk_index`); sink инжектит
  свой scope-tag при write, caller не задаёт scope извне. `narrow` на
  write-стороне не нужен — primary-key уникален.

Backend-агностично: любой impl работает поверх произвольного VectorStore.

╔═══ VectorStore-payload (primary + raw) ══════════════════════════════════════╗
║ chunk_id        — primary key (varchar в pgvector / id в Chroma)             ║
║ format_content  — `Chunk.format_content`; то что эмбедится. В Chroma →       ║
║                    `document`; в pgvector → TEXT                             ║
║ raw_content     — `Chunk.raw_content` (опционально хранится отдельной        ║
║                    колонкой; в Chroma не сохраняется — на чтение lossy       ║
║                    round-trip = format_content)                              ║
║ embedding       — vector(N), генерируется Embedder'ом на write от            ║
║                    `format_content`                                          ║
╚══════════════════════════════════════════════════════════════════════════════╝

╔═══ Metadata fields (плоский kv-блок рядом с payload) ════════════════════════╗
║ ── Chunk-frame (TrackingKeys, flat-keys, store-impl пишет всегда) ──         ║
║ source_id       — `Chunk.source_id`                                          ║
║ chunk_index     — `Chunk.chunk_index`                                        ║
║ content_hash    — `Chunk.content_hash` → fingerprint для idempotency         ║
║ updated_at      — refresh-timestamp; cleanup-фильтр (before cutoff)          ║
║ tags            — `Chunk.tags`; impl выбирает encoding (chroma: `tag.X`=true)║
║                                                                              ║
║ ── ChunkKeys (dotted; пишет format-chunker, если умеет) ─────────────        ║
║ chunk.location.start                                                         ║
║ chunk.location.end       — char/byte-offset чанка в source-документе         ║
║ chunk.anchor             — heading-id, html-id, fragment, …                  ║
║                                                                              ║
║ ── SectionKeys (dotted; пишет parser в section.metadata, проходит насквозь) ─║
║ section.location.start                                                       ║
║ section.location.end     — координаты родительской section в источнике       ║
║ section.anchor                                                               ║
║ section.heading.level                                                        ║
║ section.heading.text     — типизированные поля HeadingSection                ║
║                                                                              ║
║ ── Scope-keys (impl-specific) ───────────────────────────────────────────    ║
║ namespace*               — `NamespacedView` пишет namespace="docs"           ║
║ tenant_id*               — `TenantView` пишет tenant_id=...                  ║
║                                                                              ║
║ ── Произвольная business-Metadata (любые dotted ключи парсера/transport) ──  ║
║ transport.etag, reader.doc_type, reader.markdown.heading_text, …             ║
╚══════════════════════════════════════════════════════════════════════════════╝

Реляционный пример хранения чанков в таблице (упрощённо, типичные колонки):
┌──────────┬──────────────┬──────────────┬───────────┬────────┬──────────────┬─────────────┬───────┬───────────────────┬───────────────┬────────────┐
│ chunk_id │format_content│  embedding   │ source_id │ ch_idx │ content_hash │ updated_at  │ tags  │ chunk.location.*  │ chunk.anchor  │ namespace* │
├──────────┼──────────────┼──────────────┼───────────┼────────┼──────────────┼─────────────┼───────┼───────────────────┼───────────────┼────────────┤
│ abc123:0 │ "# Intro\\n…"│ [0.12,0.04…] │ src/p-1   │   0    │ sha256:aaa.. │ 1700000001  │ {pub} │ start=0   end=120 │ intro         │ docs       │
│ abc123:1 │ "## API\\n…" │ [0.08,0.41…] │ src/p-1   │   1    │ sha256:bbb.. │ 1700000001  │ {pub} │ start=120 end=240 │ api           │ docs       │
│ def456:0 │ "plain text" │ [0.31,0.27…] │ src/p-2   │   0    │ sha256:ccc.. │ 1700000005  │ {pdf} │ —                 │ —             │ docs       │
│ xyz789:0 │ "```py\\n…"  │ [0.55,0.13…] │ repo/foo  │   0    │ sha256:ddd.. │ 1700000010  │ {ai}  │ start=0   end=200 │ —             │ code       │
└──────────┴──────────────┴──────────────┴───────────┴────────┴──────────────┴─────────────┴───────┴───────────────────┴───────────────┴────────────┘
(embedding-вектор обрезан до 2 элементов; реально size=N. tags в chroma-impl
разворачиваются в отдельные `tag.X = true` ключи; здесь склеены для краткости.
`chunk.location.*` / `chunk.anchor` живут как dotted metadata-keys, не как
отдельные колонки — они опциональны и пишутся только когда format-chunker их
вычислил.)

Слева направо:
  Payload      : chunk_id, format_content, embedding (raw_content — опционально)
  TrackingKeys : source_id, chunk_index, content_hash, updated_at, tags
  ChunkKeys    : chunk.location.*, chunk.anchor (опционально, dotted)
  SectionKeys  : section.location.*, section.anchor, section.heading.*
                 (dotted, проходят из `section.metadata` пробросом)
  Scope-key*   : namespace* / tenant_id* / … (impl-specific)
  Business-MD  : `transport.etag`, `reader.doc_type`, `reader.markdown.*`, …
                 (произвольные dotted ключи — `Chunk.metadata.to_wire()`).
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
    """Универсальные metadata-ключи view-операций.

    Это **wire-имена** для projection top-level Chunk-полей
    (`source_id`, `chunk_index`, `content_hash`, `tags`) и tracking-полей
    (`updated_at`) в плоский metadata-store. VectorStore-impl'ы должны
    использовать эти константы при записи и чтении — один источник правды
    на всех backend'ов.
    """

    CONTENT_HASH: ClassVar[str] = "content_hash"
    UPDATED_AT: ClassVar[str] = "updated_at"
    SOURCE_ID: ClassVar[str] = "source_id"
    CHUNK_INDEX: ClassVar[str] = "chunk_index"
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
        собственные поля (`chunk_id`, `source_id`, `chunk_index`, `tags`, …);
        sink сам инжектит свой scope-tag (например `namespace="docs"`) при
        write, caller не задаёт scope извне.

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
