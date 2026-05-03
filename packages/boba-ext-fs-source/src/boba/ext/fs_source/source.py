"""FsSource: файловая система как Source."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from fnmatch import fnmatch
from pathlib import Path

from boba.ext.fs_source.config import FsSourceConfig
from boba.indexing import (
    IndexingContext,
    Source,
    SourceId,
    SourceItem,
)

__all__ = ["FsSource"]

logger = logging.getLogger(__name__)


class FsSource(Source):
    """Файловая система как Source.

    SourceItem.source_id = `fs:{abs_path}`,
    content_hint = расширение без точки (`md`, `txt`, `html`),
    content_hash = mtime (cheap, для skip-if-same).
    """

    def __init__(self, config: FsSourceConfig) -> None:
        self._config = config

    def name(self) -> str:
        return f"FsSource({len(self._config.paths)} roots)"

    def source_factory_id(self) -> SourceId:
        return SourceId("ext.fs")

    def stream(self, ctx: IndexingContext) -> Iterable[SourceItem]:
        del ctx
        for file_path in self._walk():
            yield from self._emit_item(file_path)

    def list_source_ids(self) -> Iterable[str]:
        return (f"fs:{Path(p).resolve()}" for p in self._walk())

    def _walk(self) -> Iterator[str]:
        for raw in self._config.paths:
            p = Path(raw)
            if p.is_file():
                if self._matches(p):
                    yield str(p)
                continue
            if p.is_dir():
                yield from self._walk_dir(p)
                continue
            logger.warning("path not found: %r; skipped", raw)

    def _walk_dir(self, root: Path) -> Iterator[str]:
        for f in sorted(root.rglob("*")):
            if not f.is_file():
                continue
            if not self._config.follow_symlinks and f.is_symlink():
                continue
            if any(seg.startswith(".") for seg in f.parts):
                continue
            if self._matches(f):
                yield str(f)

    def _matches(self, path: Path) -> bool:
        name = path.name
        if self._config.exclude and any(
            path.match(pat) or fnmatch(name, pat)
            for pat in self._config.exclude
        ):
            return False
        if not self._config.include:
            return True
        return any(
            path.match(pat) or fnmatch(name, pat)
            for pat in self._config.include
        )

    def _emit_item(self, file_path: str) -> Iterator[SourceItem]:
        """Yield 0..1 SourceItem; 0 — если read/stat упал."""
        p = Path(file_path)
        try:
            payload = p.read_bytes()
            mtime = str(int(p.stat().st_mtime))
        except OSError as e:
            logger.warning("read failed for %r: %s; skipped", file_path, e)
            return
        suffix = p.suffix.lstrip(".").lower() or "bin"
        yield SourceItem(
            source_id=f"fs:{p.resolve()}",
            content_hint=suffix,
            payload=payload,
            metadata={},
            content_hash=mtime,
        )
