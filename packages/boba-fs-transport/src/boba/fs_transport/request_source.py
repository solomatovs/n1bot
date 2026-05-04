"""FsWalkRequestSource: обход путей (file/dir) → поток FsRequest."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from fnmatch import fnmatch
from pathlib import Path

from boba.fs_transport.request import FsRequest
from boba.indexing import IndexingContext, RequestSource

__all__ = ["FsWalkRequestSource"]

logger = logging.getLogger(__name__)


class FsWalkRequestSource(RequestSource[FsRequest]):
    """Раскрывает paths (файлы и директории) в FsRequest'ы.

    `paths` — список файлов или директорий; директории обходятся `rglob`.
    `include` / `exclude` — glob-фильтры применяются к имени файла и относительному
    пути. Скрытые директории/файлы (`.git`, `.venv`, `.cache`) — пропускаются.
    `follow_symlinks` — false по умолчанию (защита от циклов).

    `source_id` каждого FsRequest = `fs:{absolute_path}`. RequestSource не
    знает «canonical» format'ов (Confluence-export и т.п.) — если нужна
    cross-transport дедупликация, делается отдельный RequestSource поверх.
    """

    def __init__(
        self,
        *,
        paths: Sequence[str],
        include: Sequence[str] = (),
        exclude: Sequence[str] = (),
        follow_symlinks: bool = False,
    ) -> None:
        self._paths = list(paths)
        self._include = tuple(include)
        self._exclude = tuple(exclude)
        self._follow_symlinks = follow_symlinks

    def name(self) -> str:
        return f"FsWalkRequestSource(paths={len(self._paths)})"

    def stream(self, ctx: IndexingContext) -> Iterable[FsRequest]:
        del ctx
        for path in self._iter_files():
            p = Path(path)
            yield FsRequest(
                path=str(p),
                source_id=f"fs:{p.resolve()}",
                metadata={"path": str(p), "name": p.name},
            )

    def list_source_ids(self, ctx: IndexingContext) -> Iterable[str]:
        del ctx
        for path in self._iter_files():
            yield f"fs:{Path(path).resolve()}"

    def _iter_files(self) -> Iterator[str]:
        for raw in self._paths:
            p = Path(raw)
            if p.is_file():
                if self._matches(p):
                    yield str(p)
                continue
            if p.is_dir():
                yield from self._walk_dir(p)
                continue
            logger.warning("fs path not found: %r; skipped", raw)

    def _walk_dir(self, root: Path) -> Iterator[str]:
        for f in sorted(root.rglob("*")):
            if not f.is_file():
                continue
            if not self._follow_symlinks and f.is_symlink():
                continue
            if any(seg.startswith(".") for seg in f.parts):
                continue
            if self._matches(f):
                yield str(f)

    def _matches(self, path: Path) -> bool:
        name = path.name
        if self._exclude and any(
            path.match(pat) or fnmatch(name, pat) for pat in self._exclude
        ):
            return False
        if not self._include:
            return True
        return any(
            path.match(pat) or fnmatch(name, pat) for pat in self._include
        )
