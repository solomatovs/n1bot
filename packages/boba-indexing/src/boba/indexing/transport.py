"""
Transport - абстрактный интерфейс для получения сырых данных (RawDocument) по Request

Идея в том, что бы действия, направленные на получение RawDocument
Содержались в конкретной реализации: http, fs, s3, postgres и так далее

Transport может иметь внутреннее состояние
(например, пул HTTP-соединений, открытые файловые дескрипторы и т.п.).

К примеру:
- `Transport[HttpRequest]` может выполнить HTTP-запрос
    и вернуть файловый описатель на body ответа

 - `Transport[FsRequest]` может открыть файл и вернуть файловый описатель
"""

from __future__ import annotations

from typing import TypeVar

from boba.indexing.context import PipelineContext
from boba.indexing.raw_document import RawDocument
from boba.indexing.request import Request
from boba.patterns import StreamTransformer

__all__ = ["Transport"]

ReqT = TypeVar("ReqT", bound=Request)


class Transport(
    StreamTransformer[PipelineContext, ReqT, RawDocument],
):
    """
    Преобразует поток `ReqT`-планов в поток `RawDocument` (открытых handle'ов).

    **Схема**:
    ```python
    ReqT   ───────────────────────transport.stream──→  RawDocument
        <fields: url|path|…>     ──open/fetch───────→
        source_id   : SourceId   ──pass─────────────→     source_id   (тот же)
        metadata    : Metadata   ──merge────────────→     metadata    (+ TransportKeys.ETAG / MTIME / CONTENT_TYPE …)
                                                    →     handle      : BinaryStream  (открыт; закроется по выходу из stream)
    ```

    **Контракты**:
    - Transport владеет lifecycle handle: открывает handle в generator через `with`,
      закрывает по выходу. Reader потребляет, но не закрывает
    - Один Transport работает только с одним типом Request: `Transport[HttpRequest]`
      не примет `FsRequest` и наоборот — type-checker не даст совместить
    - На I/O-проблему бросает соответствующую TransportError; pipeline
      может изолировать ошибку и продолжить со следующего Request

    **Пример** (usage `FsTransport`):
    ```python
    transport: Transport[FsRequest] = FsTransport()

    requests = iter([
        # входной path это просто входной аргумент
        # а FsRequest уже содержит логику открытия этого path
        FsRequest(
            path="/abs/note.md",
            source_id=SourceId("fs:/abs/note.md"),
            metadata=Metadata.empty().set(FsKeys.PATH, "/abs/note.md"),
        ),
    ])

    # Первый next(...) даёт открытый RawDocument; следующий next закроет handle.
    raw = next(iter(transport.stream(ctx, requests)))
    raw == RawDocument(
        handle=<BufferedReader name='/abs/note.md'>,    # новое: открытый файловый дескриптор
        source_id=SourceId("fs:/abs/note.md"),          # pass из FsRequest
        metadata=(                                      # merge из FsRequest.metadata + транспортные ключи
            Metadata.empty()
            .set(FsKeys.PATH, "/abs/note.md")           # был в FsRequest
            .set(TransportKeys.MTIME, 1715342400.0)     # добавил Transport
            .set(FsKeys.SIZE, 1024)                     # добавил Transport
            .set(FsKeys.SUFFIX, "md")                   # добавил Transport
        ),
    )
    raw.handle.read()  # → b"# Note\\n..."  (Reader потребляет до следующей итерации)
    ```
    """  # noqa: E501
