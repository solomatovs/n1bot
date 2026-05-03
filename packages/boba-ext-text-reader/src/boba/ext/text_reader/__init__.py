"""Boba indexing extension: TextReader для txt/log/text форматов."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.text_reader.reader import TextReader
from boba.indexing import IndexerExtensionContext, Reader

__all__ = ["TextReader", "register_readers"]


def register_readers(ctx: IndexerExtensionContext) -> Iterable[Reader]:
    """Entry-point boba.indexing.readers."""
    del ctx
    yield TextReader()
