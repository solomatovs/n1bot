"""Decoder — слой между Transport и Reader: декомпрессия, декодирование, нормализация payload/metadata."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import NewType

from boba.indexing.raw_document import RawDocument

__all__ = ["Decoder", "DecoderId"]


DecoderId = NewType("DecoderId", str)
"""Идентификатор Decoder-реализации."""


class Decoder(ABC):
    """RawDocument -> RawDocument: преобразование payload и/или metadata."""

    @abstractmethod
    def decoder_id(self) -> DecoderId: ...

    @abstractmethod
    def decode(
        self,
        raw: RawDocument,
    ) -> RawDocument: ...
