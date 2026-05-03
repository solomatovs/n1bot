"""Boba indexing extension: HtmlReader для html/htm/xhtml форматов."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.html_reader.reader import HtmlReader
from boba.indexing import IndexerExtensionContext, Reader

__all__ = ["HtmlReader", "register_readers"]


def register_readers(ctx: IndexerExtensionContext) -> Iterable[Reader]:
    """Entry-point boba.indexing.readers."""
    del ctx
    yield HtmlReader()
