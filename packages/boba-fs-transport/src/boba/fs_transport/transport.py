"""FsTransport: FsRequest → RawDocument через open(path, 'rb')."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from boba.fs_transport.request import FsRequest
from boba.processing import IndexingContext, RawDocument, Transport

__all__ = ["FsTransport"]


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

    def _open_one(self, req: FsRequest) -> Iterable[RawDocument]:
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
