"""Decoder: RawDocument → RawDocument — нормализация payload между Transport и Reader.

Стадия между `Transport` и `Reader`: Transport-payload не всегда совпадает
по формату с тем, что ждёт Reader (например JSON-обёртка REST-ответа над
HTML-телом). Decoder читает handle, разворачивает payload и обогащает
metadata, возвращает новый RawDocument с готовым к парсингу handle'ом.

Identity-decoder = passthrough: для pipeline'ов где Transport уже возвращает
формат, который Reader понимает (FS-обходы, прямые HTML-эндпоинты).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from boba.indexing.context import IndexingContext
from boba.indexing.raw_document import RawDocument
from boba.patterns import ContextConverter, StateFull, StrId

__all__ = ["Decoder", "DecoderId", "IdentityDecoder"]


class DecoderId(StrId):
    """Идентификатор Decoder-реализации (например 'ext.confluence_json')."""


class Decoder(
    ContextConverter[IndexingContext, RawDocument, RawDocument],
    StateFull,
    ABC,
):
    """RawDocument → RawDocument: преобразование payload и/или metadata."""

    @abstractmethod
    def decoder_id(self) -> DecoderId: ...

    @abstractmethod
    def convert(
        self, ctx: IndexingContext, value: RawDocument,
    ) -> RawDocument: ...


class IdentityDecoder(Decoder):
    """Passthrough: payload и metadata передаются как есть."""

    def name(self) -> str:
        return "IdentityDecoder"

    def decoder_id(self) -> DecoderId:
        return DecoderId("identity")

    def convert(
        self, ctx: IndexingContext, value: RawDocument,
    ) -> RawDocument:
        del ctx
        return value

    def reset(self) -> None:
        pass
