"""Порты конвейера: запрос, транспорт, reader, chunker, embedder.

Каркас асинхронный целиком: стадии соединяются async-потоками, чтобы конвейер
мог вести несколько источников сразу. Синхронная работа (разбор документа,
инференс эмбеддера) — забота реализации порта: она сама уносит её с loop'а.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
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

    Handle не закрывает (это Transport), на несовместимый payload бросает
    IncompatibleContentError, autodetect'а нет. Разбор обычно синхронный и
    тяжёлый (native-парсер, OCR) — уносить его с loop'а обязана реализация.
    """

    @abstractmethod
    def reader_id(self) -> ReaderId: ...

    @abstractmethod
    def read(self, raw: RawDocument) -> AsyncIterator[Section[T]]: ...


T = TypeVar("T")


ChunkerId = NewType("ChunkerId", str)
"""Идентификатор Chunker-реализации (например 'sliding', 'heading')."""


class Chunker(ABC, Generic[T]):
    """Преобразует поток Section[T] в поток Chunk[T].

    chunk_id детерминирован (re-index), chunk_index сквозной по source_id,
    content_hash обязан заполнить сам Chunker.
    """

    @abstractmethod
    def chunker_id(self) -> ChunkerId: ...

    @abstractmethod
    def chunk(self, sections: AsyncIterable[Section[T]]) -> AsyncIterator[Chunk[T]]: ...


T = TypeVar("T")


class Embedder(ABC, Generic[T]):
    """Преобразует content в вектор; provider-нейтральная абстракция.

    Батч эмбеддится долго: локальный backend обязан уносить инференс с loop'а,
    удалённый — просто ходит по сети.
    """

    @abstractmethod
    async def embed_documents(
        self,
        contents: Sequence[T],
    ) -> Sequence[Sequence[float]]:
        """Векторизация для индексации (потенциально с document-prefix)."""
        ...

    @abstractmethod
    async def embed_query(self, content: T) -> Sequence[float]:
        """Векторизация запроса (для асимметричных моделей — с query-prefix)."""
        ...

    @abstractmethod
    def dim(self) -> int:
        """Размерность embedding-вектора"""
        ...


@runtime_checkable
class Request(Protocol):
    """Контракт Request-DTO — чистый план «что забрать» + исходная metadata.

    source_id НЕ часть Request — его вычисляет Transport из реального адреса,
    чтобы identity не дрейфовала.
    """

    metadata: Metadata


ReqT = TypeVar("ReqT", bound=Request)


class RequestSource(ABC, Generic[ReqT]):
    """Источник Request'ов; source_id не формирует — его выводит Transport.

    Поток асинхронный: discovery сам ходит по сети (пагинация REST), и держать
    его синхронным значило бы блокировать loop на всё время обхода.
    """

    @abstractmethod
    def requests(self) -> AsyncIterator[ReqT]:
        """Сгенерировать поток ReqT-планов для Transport'а."""
        ...


ReqT = TypeVar("ReqT", bound=Request)


class Transport(ABC, Generic[ReqT]):
    """
    Достаёт RawDocument'ы (открытые handle'ы) по одному ReqT-плану.

    **Схема**:
    python
    ReqT   ───────────────────────transport.fetch──->  AsyncIterator[RawDocument]
        <fields: url|path|…>     ──open/fetch───────->
        source_id   : SourceId   ──pass─────────────->     source_id   (тот же)
        metadata    : Metadata   ──merge────────────->     metadata    (+ TransportKeys.ETAG / MTIME / CONTENT_TYPE …)
                                                    ->     handle      : BinaryStream  (открыт; закроется по выходу из fetch)


    Один request может развернуться в несколько RawDocument (например
    Confluence-страница -> HTML + вложения), поэтому выход — поток.

    **Контракты**:
    - fetch асинхронен: сеть не занимает поток, а разбор документа идёт синхронно
      в чужом исполнителе, куда открытый сетевой handle протаскивать нельзя —
      поэтому к моменту yield тело документа уже доступно целиком
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

    # Первый шаг итерации даёт RawDocument; следующий закроет handle.
    raw = await anext(aiter(transport.fetch(request)))
    raw == RawDocument(
        handle=<AsyncBinaryStream>,                     # новое: открытый поток тела
        source_id=SourceId("fs:/abs/note.md"),          # выводит FsTransport.source_id из path
        metadata=(                                      # merge из FsRequest.metadata + транспортные ключи
            Metadata.empty()
            .set(FsKeys.PATH, "/abs/note.md")           # был в FsRequest
            .set(TransportKeys.MTIME, 1715342400.0)     # добавил Transport
            .set(FsKeys.SIZE, 1024)                     # добавил Transport
            .set(FsKeys.SUFFIX, "md")                   # добавил Transport
        ),
    )
    await raw.handle.read()  # -> b"# Note\\n..."  (Reader потребляет до следующей итерации)

    """  # noqa: E501

    @abstractmethod
    def source_id(self, request: ReqT) -> SourceId:
        """
        Идентичность документа = реальный адрес запрошенного объекта
        """
        ...

    @abstractmethod
    def fetch(self, request: ReqT) -> AsyncIterator[RawDocument]: ...

    async def close(self) -> None:
        """Освободить ресурсы транспорта (соединения/пулы). По умолчанию — no-op."""

    async def __aenter__(self) -> Transport[ReqT]:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


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

    async def read(self, raw: RawDocument) -> AsyncIterator[Section[T]]:
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

        async for section in inner.read(raw):
            yield section
