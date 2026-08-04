"""Порты конвейера: запрос, транспорт, reader, chunker, embedder."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from typing import Generic, Literal, NewType, Protocol, TypeVar, runtime_checkable

from boba.indexing.chunks import Chunk
from boba.indexing.errors import IncompatibleContentError
from boba.indexing.sections import RawDocument, Section, SourceId
from boba.indexing.values import Metadata, MetadataKey

__all__ = [
    "Chunker",
    "ChunkerId",
    "DispatchReader",
    "Embedder",
    "Reader",
    "ReaderId",
    "Request",
    "RequestSource",
    "Transport",
]

T = TypeVar("T")


ReaderId = NewType("ReaderId", str)
"""Идентификатор Reader-реализации (например ext.text, ext.markdown)."""


class Reader(ABC, Generic[T]):
    """Разбирает RawDocument на Section[T].

    Handle не закрывает (это Transport), на несовместимый payload бросает IncompatibleContentError, autodetect'а нет.
    """

    @abstractmethod
    def reader_id(self) -> ReaderId: ...

    @abstractmethod
    def read(self, raw: RawDocument) -> Iterable[Section[T]]: ...

T = TypeVar("T")


ChunkerId = NewType("ChunkerId", str)
"""Идентификатор Chunker-реализации (например 'sliding', 'heading')."""


class Chunker(ABC, Generic[T]):
    """Преобразует поток Section[T] в поток Chunk[T].

    chunk_id детерминирован (re-index), chunk_index сквозной по source_id, content_hash обязан заполнить сам Chunker.
    """

    @abstractmethod
    def chunker_id(self) -> ChunkerId: ...

    @abstractmethod
    def chunk(self, sections: Iterable[Section[T]]) -> Iterable[Chunk[T]]: ...

T = TypeVar("T")


class Embedder(ABC, Generic[T]):
    """Преобразует content в вектор; provider-нейтральная абстракция."""

    @abstractmethod
    def embed_documents(self, contents: Iterable[T]) -> Iterable[Sequence[float]]:
        """Векторизация для индексации (потенциально с document-prefix)."""
        ...

    @abstractmethod
    def embed_query(self, content: T) -> Sequence[float]:
        """Векторизация запроса (для асимметричных моделей — с query-prefix)."""
        ...

    @abstractmethod
    def dim(self) -> int:
        """Размерность embedding-вектора"""
        ...

@runtime_checkable
class Request(Protocol):
    """Контракт Request-DTO — чистый план «что забрать» + исходная metadata.

    source_id НЕ часть Request — его вычисляет Transport из реального адреса, чтобы identity не дрейфовала.
    """

    @property
    def metadata(self) -> Metadata: ...


ReqT = TypeVar("ReqT", bound=Request)


class RequestSource(ABC, Generic[ReqT]):
    """Источник Request'ов для Transport'а; source_id не формирует — его выводит Transport."""

    @abstractmethod
    def requests(self) -> Iterable[ReqT]:
        """Сгенерировать поток ReqT-планов для Transport'а."""
        ...

ReqT = TypeVar("ReqT", bound=Request)


class Transport(ABC, Generic[ReqT]):
    """
    Достаёт RawDocument'ы (открытые handle'ы) по одному ReqT-плану.

    **Схема**:
    python
    ReqT   ───────────────────────transport.fetch──->  Iterable[RawDocument]
        <fields: url|path|…>     ──open/fetch───────->
        source_id   : SourceId   ──pass─────────────->     source_id   (тот же)
        metadata    : Metadata   ──merge────────────->     metadata    (+ TransportKeys.ETAG / MTIME / CONTENT_TYPE …)
                                                    ->     handle      : BinaryStream  (открыт; закроется по выходу из fetch)


    Один request может развернуться в несколько RawDocument (например
    Confluence-страница -> HTML + вложения), поэтому выход — Iterable.

    **Контракты**:
    - Transport владеет lifecycle handle: открывает handle в generator через with,
      закрывает по выходу из fetch. Reader потребляет, но не закрывает
    - Один Transport работает только с одним типом Request: Transport[HttpRequest]
      не примет FsRequest и наоборот — type-checker не даст совместить
    - На I/O-проблему бросает соответствующую TransportError; pipeline
      может изолировать ошибку и продолжить со следующего Request

    **Пример** (usage FsTransport):
    python
    transport: Transport[FsRequest] = FsTransport()

    # входной path это просто входной аргумент
    # а FsRequest уже содержит логику открытия этого path
    request = FsRequest(
        path="/abs/note.md",
        metadata=Metadata.empty().set(FsKeys.PATH, "/abs/note.md"),
    )

    # Первый next(...) даёт открытый RawDocument; следующий next закроет handle.
    raw = next(iter(transport.fetch(request)))
    raw == RawDocument(
        handle=<BufferedReader name='/abs/note.md'>,    # новое: открытый файловый дескриптор
        source_id=SourceId("fs:/abs/note.md"),          # выводит FsTransport.source_id из path
        metadata=(                                      # merge из FsRequest.metadata + транспортные ключи
            Metadata.empty()
            .set(FsKeys.PATH, "/abs/note.md")           # был в FsRequest
            .set(TransportKeys.MTIME, 1715342400.0)     # добавил Transport
            .set(FsKeys.SIZE, 1024)                     # добавил Transport
            .set(FsKeys.SUFFIX, "md")                   # добавил Transport
        ),
    )
    raw.handle.read()  # -> b"# Note\\n..."  (Reader потребляет до следующей итерации)

    """  # noqa: E501

    @abstractmethod
    def source_id(self, request: ReqT) -> SourceId:
        """
        Идентичность документа = реальный адрес запрошенного объекта
        """
        ...

    @abstractmethod
    def fetch(self, request: ReqT) -> Iterable[RawDocument]: ...

    def close(self) -> None:
        """Освободить ресурсы транспорта (соединения/пулы). По умолчанию — no-op."""

    def __enter__(self) -> Transport[ReqT]:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

T = TypeVar("T")


class DispatchReader(Reader[T]):
    """Reader[T], делегирующий sub-Reader'у из routes по значению metadata-ключа by."""

    def __init__(
        self,
        *,
        by: MetadataKey[str],
        routes: Mapping[str, Reader[T]],
        reader_id: ReaderId,
        on_unknown: Literal["error", "skip"] = "error",
    ) -> None:
        if not routes:
            msg = "DispatchReader: routes must be non-empty"
            raise ValueError(msg)
        self._by = by
        self._routes = dict(routes)
        self._reader_id = reader_id
        self._on_unknown = on_unknown

    def reader_id(self) -> ReaderId:
        return self._reader_id

    def read(self, raw: RawDocument) -> Iterable[Section[T]]:
        key_value = raw.metadata.get(self._by)
        if key_value is None:
            if self._on_unknown == "skip":
                return
            raise IncompatibleContentError(
                reader_id=str(self._reader_id),
                canonical_id=str(raw.source_id),
                reason=(
                    f"metadata key {self._by.name!r} is missing; "
                    f"DispatchReader cannot pick a sub-reader"
                ),
            )

        inner = self._routes.get(key_value)
        if inner is None:
            if self._on_unknown == "skip":
                return
            supported = ", ".join(sorted(self._routes))
            raise IncompatibleContentError(
                reader_id=str(self._reader_id),
                canonical_id=str(raw.source_id),
                reason=(
                    f"no sub-reader for {self._by.name}={key_value!r}; "
                    f"known: [{supported}]"
                ),
            )

        yield from inner.read(raw)
