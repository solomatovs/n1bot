"""
RuntimePipeline: однопроходный pipeline без индексации.

Симметрия со StreamingIndexer: тот же набор стадий (RequestSource → Transport →
Decoder → Reader), но без Chunker/IndexSink — caller сам решает, что делать
с потоком Section'ов (собирать в JsonResult, печатать в stdout, …).

Назначение — tool и CLI-команды, которым нужно получить
структурированный ответ из удалённого источника без записи в индекс.

`Decoder` опционален; по умолчанию — `PassThroughDecoder`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Generic, TypeVar

from boba.indexing.context import PipelineContext
from boba.indexing.decoder import Decoder
from boba.indexing.reader import Reader
from boba.indexing.request import Request, RequestSource
from boba.indexing.sections import Section
from boba.indexing.transport import Transport

__all__ = ["RuntimePipeline"]

ReqT = TypeVar("ReqT", bound=Request)
T = TypeVar("T")


class RuntimePipeline(Generic[ReqT, T]):
    """Однопроходный pipeline: `ReqT` → `RawDocument` → `Section[T]`."""

    def __init__(
        self,
        *,
        request_source: RequestSource[ReqT],
        transport: Transport[ReqT],
        reader: Reader[T],
        decoders: Sequence[Decoder] = (),
    ) -> None:
        self._request_source = request_source
        self._transport = transport
        self._reader = reader
        self._decoders: tuple[Decoder, ...] = tuple(decoders)

    def stream(self, ctx: PipelineContext) -> Iterator[Section[T]]:
        for request in self._request_source.stream(ctx):
            for raw in self._transport.stream(ctx, [request]):
                decoded = raw
                for decoder in self._decoders:
                    decoded = decoder.convert(decoded)
                yield from self._reader.convert(decoded)
