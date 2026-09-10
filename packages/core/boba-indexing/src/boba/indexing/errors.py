"""Generic ошибки processing-слоя."""

from __future__ import annotations

__all__ = [
    "IncompatibleContentError",
    "IndexingError",
    "SourceGoneError",
    "TransportError",
]


class IndexingError(Exception):
    """База ошибок processing-домена (любая стадия
    Source/Transport/Reader/Decoder/Chunker/Store).
    """


class IncompatibleContentError(IndexingError):
    """Reader не может распарсить RawDocument.

    Не transient, а ошибка конфигурации pipeline — падать после первой же такой ошибки.
    """

    def __init__(self, reader_id: str, canonical_id: str, reason: str) -> None:
        super().__init__(
            f"reader {reader_id!r} cannot parse canonical_id={canonical_id!r}: {reason}"
        )
        self.reader_id = reader_id
        self.canonical_id = canonical_id
        self.reason = reason


class TransportError(IndexingError):
    """Transport не смог забрать источник: сеть, статус ответа, обрыв тела.

    Transient по природе: pipeline изолирует источник и идёт к следующему.
    """


class SourceGoneError(TransportError):
    """Источника у источника данных больше нет: он ответил «не найдено».

    Не догадка очистки, а прямой ответ: конвейер снимает такой источник
    вместе с чанками и детьми, не дожидаясь конца обхода.
    """
