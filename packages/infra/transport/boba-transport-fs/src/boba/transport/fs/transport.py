"""FsTransport — открывает файл и возвращает RawDocument с заполненной metadata.

Lifecycle handle: with open(path, "rb") as fp: yield RawDocument(handle=fp, ...).
Reader должен прочитать handle ДО следующей итерации generator'а — после
возврата control'а with-блок закроет file.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from boba.indexing import (
    RawDocument,
    SourceId,
    Transport,
    TransportKeys,
)
from boba.transport.fs.keys import FsKeys
from boba.transport.fs.request import FsRequest

__all__ = ["FsTransport"]

_log = logging.getLogger(__name__)


class FsTransport(Transport[FsRequest]):
    """
    Transport[FsRequest]: FsRequest -> RawDocument через open(path, "rb").

    **Схема**:
    python
    FsRequest   ──────────────────────FsTransport.fetch──->  RawDocument
        path        : str        ──open────────────────->
                                 ──source_id(req)───────->     source_id   (`fs:{path}` — выводит транспорт)
        metadata    : Metadata   ──merge───────────────->     metadata    (+ TransportKeys.MTIME, FsKeys.SIZE, FsKeys.SUFFIX)
                                                           ->     handle      : BufferedReader  (open; закроется по выходу из fetch)
    

    **Lifecycle handle**:
    python
    with p.open("rb") as fp:
        yield RawDocument(handle=fp, ...)        # handle жив
    # сюда возвращаемся после next(generator) — fp закрыт
    
    Reader должен прочитать handle ДО следующей итерации, иначе уже
    закрытый fp.read() бросит ValueError.

    **Поведение на ошибки**:
    - файл исчез между листингом и open (OSError на stat) — warn'аем
      и пропускаем (request «съедается», prowadzення продолжается).
    - суффикс приводится к lower(); пустой -> "bin".

    **Пример**:
    python
    transport = FsTransport()
    request = FsRequest(
        path="/abs/note.md",
        metadata=Metadata.empty().set(FsKeys.PATH, "/abs/note.md"),
    )

    # 1 FsRequest -> 1 открытый RawDocument; handle живёт до перехода к следующему.
    raw = next(iter(transport.fetch(request)))
    raw == RawDocument(
        handle=<BufferedReader name='/abs/note.md'>,   # новое: открытый file descriptor
        source_id=SourceId("fs:/abs/note.md"),         # выводит FsTransport.source_id = fs:{path}
        metadata=(                                     # merge из FsRequest.metadata + 3 ключа от Transport
            Metadata.empty()
            .set(FsKeys.PATH, "/abs/note.md")          # был в FsRequest
            .set(TransportKeys.MTIME, 1715342400.0)    # новое от Transport (st_mtime)
            .set(FsKeys.SIZE, 1024)                    # новое от Transport (st_size)
            .set(FsKeys.SUFFIX, "md")                  # новое от Transport (lower-case extension)
        ),
    )
    raw.handle.read()  # -> b"# Note\\n..."
    
    """  # noqa: E501

    def source_id(self, request: FsRequest) -> SourceId:
        """Идентичность файла = `fs:{path}` (реальный путь запрошенного объекта)."""
        return SourceId(f"fs:{request.path}")

    def fetch(
        self,
        request: FsRequest,
    ) -> Iterable[RawDocument]:
        yield from self._open_one(request, self.source_id(request))

    @staticmethod
    def _open_one(req: FsRequest, source_id: SourceId) -> Iterable[RawDocument]:
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
                source_id=source_id,
                metadata=meta,
            )
