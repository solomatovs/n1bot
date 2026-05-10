"""FsTransport — открывает файл и возвращает RawDocument с заполненной metadata.

Lifecycle handle: `with open(path, "rb") as fp: yield RawDocument(handle=fp, ...)`.
Reader должен прочитать handle ДО следующей итерации generator'а — после
возврата control'а with-блок закроет file.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from boba.indexing import (
    PipelineContext,
    RawDocument,
    Transport,
    TransportKeys,
)
from boba.transport.fs.keys import FsKeys
from boba.transport.fs.request import FsRequest

__all__ = ["FsTransport"]

_log = logging.getLogger(__name__)


class FsTransport(Transport[FsRequest]):
    """Открывает файлы файловой системы для индексации."""

    def name(self) -> str:
        return "FsTransport"

    def stream(
        self,
        ctx: PipelineContext,
        stream: Iterable[FsRequest],
    ) -> Iterable[RawDocument]:
        del ctx
        for req in stream:
            yield from self._open_one(req)

    @staticmethod
    def _open_one(req: FsRequest) -> Iterable[RawDocument]:
        p = Path(req.path)
        try:
            stat = p.stat()
        except OSError:
            # Файл исчез между листингом и open — пропускаем; не раним прогон.
            _log.warning("fs file disappeared: %s", req.path)
            return
        suffix = p.suffix.lstrip(".").lower() or "bin"
        meta = (
            req.metadata
            .set(TransportKeys.MTIME, float(stat.st_mtime))
            .set(FsKeys.SIZE, int(stat.st_size))
            .set(FsKeys.SUFFIX, suffix)
        )
        with p.open("rb") as fp:
            yield RawDocument(
                handle=fp,
                source_id=req.source_id,
                metadata=meta,
            )
