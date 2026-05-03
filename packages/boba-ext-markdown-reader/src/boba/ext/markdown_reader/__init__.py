"""Boba indexing extension: MarkdownReader для md/markdown форматов."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.markdown_reader.reader import MarkdownReader
from boba.indexing import IndexerExtensionContext, Reader

__all__ = ["MarkdownReader", "register_readers"]


def register_readers(ctx: IndexerExtensionContext) -> Iterable[Reader]:
    """Entry-point boba.indexing.readers."""
    del ctx
    yield MarkdownReader()
