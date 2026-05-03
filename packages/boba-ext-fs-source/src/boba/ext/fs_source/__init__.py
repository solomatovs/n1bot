"""Boba indexing extension: FsSource — обход файловой системы."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.fs_source.config import (
    FsSourceConfig,
    FsSourceConfigSection,
)
from boba.ext.fs_source.factory import FsSourceFactory
from boba.ext.fs_source.source import FsSource
from boba.indexing import IndexerExtensionContext, SourceFactory

__all__ = [
    "FsSource",
    "FsSourceConfig",
    "FsSourceConfigSection",
    "FsSourceFactory",
    "register_sources",
]


def register_sources(
    ctx: IndexerExtensionContext,
) -> Iterable[SourceFactory]:
    """Entry-point boba.indexing.sources."""
    del ctx
    yield FsSourceFactory()
