"""Workspace-aware RequestSource + Transport для KbDoc-ingest.

Не пробивает изоляцию WorkspaceShell (host-пути наружу не уходят). Шаги
индексатора получают workspace-relative FsRequest.path; чтение происходит
через shell.read_binary(rel).

source_id стабильно по паре (workspace_id, rel-path) — повторный аплоад
файла с тем же именем в ту же сессию обновит чанки, не задвоит.

Применение:
    src = WorkspaceWalkRequestSource(shell=shell, paths=["upload"], include=["*.md"])
    tr  = WorkspaceTransport(shell=shell)
    pipeline = Pipeline(source=src, transport=tr, reader=KbDocReader())
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from fnmatch import fnmatch
from typing import Any

from boba.indexing import (
    Metadata,
    RawDocument,
    RequestSource,
    SourceId,
    Transport,
    TransportKeys,
)
from boba.transport.fs.keys import FsKeys
from boba.transport.fs.request import FsRequest
from boba.workspace.contract import WorkspaceError, WorkspaceShell

__all__ = ["WorkspaceTransport", "WorkspaceWalkRequestSource"]

_log = logging.getLogger(__name__)


class WorkspaceWalkRequestSource(RequestSource[FsRequest]):
    """RequestSource[FsRequest] поверх WorkspaceShell.

    Раскрывает workspace-relative paths (файлы и/или директории) в поток
    FsRequest с workspace-relative path. Директории обходятся через
    shell.tree(), файлы фильтруются include/exclude через fnmatch
    по basename. source_id = ws:{workspace_id}:{rel}.
    """

    def __init__(
        self,
        *,
        shell: WorkspaceShell[Any],
        paths: Sequence[str],
        include: Sequence[str] = (),
        exclude: Sequence[str] = (),
    ) -> None:
        self._shell = shell
        self._paths = list(paths)
        self._include = tuple(include)
        self._exclude = tuple(exclude)
        self._workspace_id = str(shell.workspace_id)

    def requests(self) -> Iterable[FsRequest]:
        for rel in self._iter_files():
            yield FsRequest(
                path=rel,
                source_id=SourceId(f"ws:{self._workspace_id}:{rel}"),
                metadata=(
                    Metadata.empty()
                    .set(FsKeys.PATH, rel)
                    .set(FsKeys.NAME, rel.rsplit("/", 1)[-1])
                ),
            )

    def _iter_files(self) -> Iterator[str]:
        for raw in self._paths:
            try:
                meta = self._shell.meta(raw)
            except WorkspaceError:
                _log.warning("workspace path not found: %r; skipped", raw)
                continue
            if meta.kind == "file":
                if self._matches(raw):
                    yield meta.path
                continue
            if meta.kind == "directory":
                yield from self._walk_dir(raw)
                continue
            _log.warning("workspace path not file/dir: %r kind=%s", raw, meta.kind)

    def _walk_dir(self, root: str) -> Iterator[str]:
        for entry in sorted(self._shell.tree(root), key=lambda e: e.path):
            if entry.kind != "file":
                continue
            if self._matches(entry.path):
                yield entry.path

    def _matches(self, rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if self._exclude and any(
            fnmatch(rel, pat) or fnmatch(name, pat) for pat in self._exclude
        ):
            return False
        if not self._include:
            return True
        return any(fnmatch(rel, pat) or fnmatch(name, pat) for pat in self._include)


class WorkspaceTransport(Transport[FsRequest]):
    """Transport[FsRequest] поверх WorkspaceShell.read_binary + .meta.

    Открывает workspace-relative FsRequest.path через shell.read_binary
    и обогащает metadata теми же ключами, что и FsTransport
    (TransportKeys.MTIME, FsKeys.SIZE, FsKeys.SUFFIX). Lifecycle
    handle'а — with shell.read_binary(rel) as fh: yield ....
    """

    def __init__(self, *, shell: WorkspaceShell[Any]) -> None:
        self._shell = shell

    def fetch(
        self,
        request: FsRequest,
    ) -> Iterable[RawDocument]:
        yield from self._open_one(request)

    def _open_one(self, req: FsRequest) -> Iterable[RawDocument]:
        try:
            entry_meta = self._shell.meta(req.path)
        except WorkspaceError:
            _log.warning("workspace file disappeared: %s", req.path)
            return
        suffix = req.path.rsplit(".", 1)
        ext = suffix[1].lower() if len(suffix) == 2 else "bin"  # noqa: PLR2004
        meta = (
            req.metadata
            .set(TransportKeys.MTIME, entry_meta.modified.timestamp())
            .set(FsKeys.SIZE, int(entry_meta.size))
            .set(FsKeys.SUFFIX, ext or "bin")
        )
        with self._shell.read_binary(req.path) as fh:
            yield RawDocument(
                handle=fh,
                source_id=req.source_id,
                metadata=meta,
            )
