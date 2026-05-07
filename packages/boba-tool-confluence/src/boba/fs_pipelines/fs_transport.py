"""FS transport stage: FsRequest + FsTransport + FsWalkRequestSource.

Три тесно связанные сущности одной FS-стадии pipeline'а:
- `FsRequest` — DTO (path + canonical source_id + metadata).
- `FsWalkRequestSource` — обходит paths и порождает поток FsRequest.
- `FsTransport` — открывает файл, отдаёт RawDocument.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from boba.processing import (
    IndexingContext,
    RawDocument,
    RequestSource,
    Transport,
)

__all__ = ["FsRequest", "FsTransport", "FsWalkRequestSource"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FsRequest:
    """План open(file). Только path; auth/headers неприменимы для FS.

    `source_id` — RequestSource ставит canonical (например `fs:/abs/path`
    или другой схемой, если файл — это нечто known-канонически). Если пуст,
    Transport заполнит как `fs:/abs/path` (resolve'ит path).

    `metadata` — обогащение для Section.metadata (например relative_path,
    space_key для FS-export Confluence и т.п.).
    """

    path: str
    source_id: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


class FsTransport(Transport[FsRequest]):
    """Открывает файлы файловой системы для индексации.

    Lifecycle handle: `with open(path, 'rb') as fp: yield RawDocument(...)`.
    Reader должен прочитать handle ДО следующей итерации generator'а —
    после возврата control'а with-блок закроет file.
    """

    def name(self) -> str:
        return "FsTransport"

    def stream(
        self,
        ctx: IndexingContext,
        stream: Iterable[FsRequest],
    ) -> Iterable[RawDocument]:
        del ctx
        for req in stream:
            yield from self._open_one(req)

    @staticmethod
    def _open_one(req: FsRequest) -> Iterable[RawDocument]:
        if not req.source_id:
            msg = (
                "FsRequest.source_id must be set by RequestSource — "
                "Transport не формирует identity, только исполняет open(). "
                f"path={req.path!r}"
            )
            raise ValueError(msg)
        p = Path(req.path)
        try:
            stat = p.stat()
        except OSError:
            # Файл исчез между листингом и open — пропускаем; не раним прогон.
            return
        suffix = p.suffix.lstrip(".").lower() or "bin"
        merged_meta: dict[str, str] = {
            **req.metadata,
            "mtime": str(int(stat.st_mtime)),
            "size": str(stat.st_size),
        }
        with p.open("rb") as fp:
            yield RawDocument(
                handle=fp,
                source_id=req.source_id,
                content_hint=suffix,
                metadata=merged_meta,
            )


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
