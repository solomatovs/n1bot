"""FsWalkRequestSource — раскрывает paths (файлы и директории) в FsRequest'ы."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from fnmatch import fnmatch
from pathlib import Path

from boba.indexing import (
    Metadata,
    RequestSource,
)
from boba.transport.fs.keys import FsKeys
from boba.transport.fs.request import FsRequest

__all__ = ["FsWalkRequestSource"]

_log = logging.getLogger(__name__)


class FsWalkRequestSource(RequestSource[FsRequest]):
    """
    RequestSource[FsRequest]: раскрывает paths (файлы/директории) в FsRequest'ы.

    **Схема**:
    
    paths=[Path("docs/")]
        └── docs/
            ├── intro.md          ─┐
            ├── api.md            ─┤  rglob("*") -> fnmatch(include/exclude)
            ├── notes/draft.md    ─┤
            └── .git/HEAD          │  скрытые dir'ы (".git" / ".venv" / …) пропускаются
                                   ▼
    ──source.requests()──->
        FsRequest(path="docs/intro.md",       metadata={FsKeys.PATH, FsKeys.NAME})
        FsRequest(path="docs/api.md",         metadata=…)
        FsRequest(path="docs/notes/draft.md", metadata=…)


    **Параметры**:
    - paths            — файлы или директории; директории обходятся rglob.
    - include/exclude — glob-фильтры по имени и пути; работают через
      Path.match ИЛИ fnmatch(name, pattern) (любой match считается).
    - follow_symlinks  — False по умолчанию (защита от циклов).

    source_id (`fs:{path}`) проставляет FsTransport, а не source. RequestSource
    несёт только path + metadata; canonical-format'ы (Confluence-export и т.п.)
    тут не знают — для cross-transport дедупа делается отдельный RequestSource.

    **Пример**:
    python
    source = FsWalkRequestSource(
        paths=["docs/"],
        include=["*.md"],
        exclude=["**/draft*"],
    )

    # 2 .md-файла отобраны (draft.md отфильтрован exclude'ом).
    list(source.requests()) == [
        FsRequest(
            path="docs/api.md",
            metadata=(
                Metadata.empty()
                .set(FsKeys.PATH, "docs/api.md")
                .set(FsKeys.NAME, "api.md")
            ),
        ),
        FsRequest(
            path="docs/intro.md",
            metadata=(
                Metadata.empty()
                .set(FsKeys.PATH, "docs/intro.md")
                .set(FsKeys.NAME, "intro.md")
            ),
        ),
    ]
    # source_id (fs:/abs/docs/...) проставит FsTransport.source_id

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

    def requests(self) -> Iterable[FsRequest]:
        for path in self._iter_files():
            p = Path(path)
            yield FsRequest(
                path=str(p),
                metadata=(
                    Metadata.empty().set(FsKeys.PATH, str(p)).set(FsKeys.NAME, p.name)
                ),
            )

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
            _log.warning("fs path not found: %r; skipped", raw)

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
        return any(path.match(pat) or fnmatch(name, pat) for pat in self._include)
