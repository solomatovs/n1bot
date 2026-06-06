"""boba.transport.fs — FS transport для индексации.

Стадии:
- `FsWalkRequestSource(paths, include, exclude, follow_symlinks)` — обходит
  файловую систему, эмитит `FsRequest`'ы.
- `FsTransport()` — открывает файл по `FsRequest.path`, отдаёт `RawDocument`
  с handle и заполненной metadata (mtime, size, suffix).
- `FsRequest(path, source_id, metadata)` — DTO между source и transport.

Использование:
    src = FsWalkRequestSource(paths=["/data/docs"], include=["*.md", "*.txt"])
    transport = FsTransport()
    pipeline = Pipeline(source=src, transport=transport, reader=MarkdownReader())
    stats = pipeline.run(ctx, chunker=chunker, sink=view, query=view, config=config)
"""

from __future__ import annotations

from boba.transport.fs.keys import FsKeys
from boba.transport.fs.request import FsRequest
from boba.transport.fs.source import FsWalkRequestSource
from boba.transport.fs.transport import FsTransport

__all__ = [
    "FsKeys",
    "FsRequest",
    "FsTransport",
    "FsWalkRequestSource",
]
