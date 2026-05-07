"""Transport: generic I/O — Request → RawDocument.

Generic по типу Request'а: `Transport[HttpRequest]` (HttpTransport)
выполняет HTTP-запрос; `Transport[FsRequest]` (FsTransport) открывает
файл. Type-checker гарантирует совместимость с RequestSource[ReqT] и
Pipeline[ReqT].

Контракт lifecycle handle: Transport открывает `with`-блоком ВНУТРИ
generator'а ПЕРЕД yield, закрывает ПОСЛЕ возврата control'а в generator
(после yield, до следующей итерации). Reader должен прочитать handle
синхронно — pipeline.run этому соответствует.
"""

from __future__ import annotations

from abc import ABC
from typing import Generic, TypeVar

from boba.patterns import StreamTransformer
from boba.processing.context import IndexingContext
from boba.processing.raw_document import RawDocument
from boba.processing.request import Request

__all__ = ["Transport"]

ReqT = TypeVar("ReqT", bound=Request)


class Transport(
    StreamTransformer[IndexingContext, ReqT, RawDocument],
    ABC,
    Generic[ReqT],
):
    """Generic I/O для Request[ReqT] → RawDocument."""
