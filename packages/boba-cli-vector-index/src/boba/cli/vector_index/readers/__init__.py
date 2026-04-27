"""Реестр reader'ов: file extension → Reader."""

from __future__ import annotations

import importlib
from pathlib import Path

from boba.cli.vector_index.readers.base import Document, Reader
from boba.cli.vector_index.readers.md import MarkdownReader
from boba.cli.vector_index.readers.txt import TextReader

__all__ = ["Document", "Reader", "UnsupportedFormatError", "reader_for"]


_BUILTIN_READERS: tuple[Reader, ...] = (
    MarkdownReader(),
    TextReader(),
)


class UnsupportedFormatError(Exception):
    """Расширение файла не покрывается ни одним reader'ом."""

    def __init__(self, path: str) -> None:
        suffix = Path(path).suffix or "(no suffix)"
        super().__init__(
            f"no reader registered for {suffix!r} ({path}). "
            f"Install extras: e.g. "
            f"`pip install boba-cli-vector-index[html]` or [confluence]."
        )
        self.path = path


def reader_for(path: str) -> Reader:
    """Найти reader по расширению; UnsupportedFormatError если нет подходящего."""
    suffix = Path(path).suffix.lower()
    for r in _BUILTIN_READERS:
        if suffix in r.extensions:
            return r
    extras_reader = _try_extras_reader(suffix)
    if extras_reader is not None:
        return extras_reader
    raise UnsupportedFormatError(path)


def _try_extras_reader(suffix: str) -> Reader | None:
    """Лениво импортировать optional reader; None если deps не установлены."""
    if suffix in (".html", ".htm"):
        try:
            module = importlib.import_module("boba.cli.vector_index.readers.html")
        except ImportError:
            return None
        return module.HtmlReader()
    return None
