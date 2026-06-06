"""
Decoder — это слой между Transport и Reader
который отвечает за преобразование сырых данных перед непосредственным чтением

Decoer это преобразования которые полезны для таких кейсов, как:

    - `gzip / brotli / zstd / deflate` — содержимое handle прозрачно проходит
        через декомпрессор, на выходе тот же тип контента, просто без сжатия
    - `base64 / hex` - декодирование
    - `encoding normalization` - utf-16 → utf-8 байт-стрим
    - `JSON-envelope extraction` — {"status":"ok","data":"<html>..."}
        распаковываем HTML вложение из json-обёртки и передаём дальше Reader
    - `PGP/age decryption` — encrypted bytes → plaintext bytes
    - `Content-Type normalization` — например, для HTML от Confluence, который приходит
        с content-type text/plain, но на самом деле является HTML
        Decoder может исправить content-type в metadata, чтобы Reader'у было проще
        принять решение о том, как парсить содержимое
"""

from __future__ import annotations

from abc import abstractmethod
from typing import NewType

from boba.indexing.raw_document import RawDocument
from boba.patterns import Converter, StateFull

__all__ = ["Decoder", "DecoderId"]


DecoderId = NewType("DecoderId", str)
"""Идентификатор Decoder-реализации."""


class Decoder(
    Converter[RawDocument, RawDocument],
    StateFull,
):
    """RawDocument → RawDocument: преобразование payload и/или metadata."""

    @abstractmethod
    def decoder_id(self) -> DecoderId: ...

    @abstractmethod
    def convert(
        self,
        value: RawDocument,
    ) -> RawDocument: ...
