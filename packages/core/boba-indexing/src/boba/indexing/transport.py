"""
Transport - абстрактный интерфейс для получения сырых данных (RawDocument) по Request

Идея в том, что бы действия, направленные на получение RawDocument
Содержались в конкретной реализации: http, fs, s3, postgres и так далее

Transport может иметь внутреннее состояние
(например, пул HTTP-соединений, открытые файловые дескрипторы и т.п.).

К примеру:
- Transport[HttpRequest] может выполнить HTTP-запрос
    и вернуть файловый описатель на body ответа

 - Transport[FsRequest] может открыть файл и вернуть файловый описатель
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Generic, TypeVar

from boba.indexing.raw_document import RawDocument
from boba.indexing.request import Request
from boba.indexing.sections import SourceId

__all__ = ["Transport"]

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
