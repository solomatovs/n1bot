"""Boba indexing extension: ConfluenceReader (heading-aware)."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.confluence_reader.reader import ConfluenceReader
from boba.indexing import IndexerExtensionContext, Reader

__all__ = ["ConfluenceReader", "register_readers"]


def register_readers(ctx: IndexerExtensionContext) -> Iterable[Reader]:
    """Entry-point boba.indexing.readers."""
    del ctx
    yield ConfluenceReader()
