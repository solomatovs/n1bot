"""Generic ошибки processing-слоя."""

from __future__ import annotations

__all__ = [
    "IncompatibleContentError",
    "IndexingError",
]


class IndexingError(Exception):
    """
    База ошибок processing-домена.

    Обычно сигнализирует о проблемах конфигурации pipeline
    или ошибках в реализации индексации
    любая ошибка на стадии Source/Transport/Reader/Decoder/Chunker/Store
    """


class IncompatibleContentError(IndexingError):
    """
    Reader получил RawDocument, который он не может распарсить.

    Сигнализирует ошибку сборки pipeline:
    - transport отдал не тот content
    - RequestSource генерит неверные URL'ы.

    pipeline должен упасть после первой же такой ошибки,
    так как это не transient ошибка, а ошибка конфигурации
    оператор должен исправить конфигурацию
    """

    def __init__(self, reader_id: str, canonical_id: str, reason: str) -> None:
        super().__init__(
            f"reader {reader_id!r} cannot parse canonical_id={canonical_id!r}: {reason}"
        )
        self.reader_id = reader_id
        self.canonical_id = canonical_id
        self.reason = reason


