"""DI-провайдеры KB-плагина — только stateless reader'ы.

Reader'ы — простые singleton'ы без конфига; шарятся между ingest-tools
(files_ingest, confluence_*_ingest) через DI. Они не зависят от подключений
или схемы.

Сервисы (`PostgresChunkStore`, `PostgresCollectionsStore`,
`PostgresKnowledgeBase`, `SqlExecutor`, `PgFtsKnowledgeBase`), embedder
и chunker — НЕ через DI. Каждый tool строит их inline из своего
tool-конфига через factory-helpers:
- `open_kb_pool(connection)` — для pool.
- `build_embedder(embedding)` — для Embedder.
- `build_chunker(chunker)` — для StructuralChunker.

Pool singleton'ится по DSN через `PostgresPool.get(...)` — повторные
вызовы `open_kb_pool` с тем же `PostgresConnection` вернут тот же pool.
"""

from __future__ import annotations

from typing import Annotated

from boba.html import HtmlReader
from boba.indexing import DispatchReader, ReaderId
from boba.kbdoc import KbDocReader
from boba.tools import FromDI, Scope, provides
from boba.transport.fs import FsKeys

__all__ = [
    "provide_dispatch_reader",
    "provide_html_reader",
    "provide_kbdoc_reader",
]

_DISPATCH_READER_ID: ReaderId = ReaderId("postgres-kb-dispatch")


@provides(scope=Scope.APP)
def provide_kbdoc_reader() -> KbDocReader:
    """KbDoc-формат (`**key:** value` header + body). Один файл = одна Section."""
    return KbDocReader()


@provides(scope=Scope.APP)
def provide_html_reader() -> HtmlReader:
    """Структурный HTML-Reader (типизированные Section'ы по heading)."""
    return HtmlReader()


@provides(scope=Scope.APP)
def provide_dispatch_reader(
    kbdoc_reader: Annotated[KbDocReader, FromDI(Scope.APP)],
    html_reader: Annotated[HtmlReader, FromDI(Scope.APP)],
) -> DispatchReader[str]:
    """DispatchReader по `FsKeys.SUFFIX` (значения от `FsTransport`).

    Поддерживаемые форматы:
    - `md`        → KbDocReader (header + body как одна Section)
    - `html/htm`  → HtmlReader (heading-aware)
    """
    return DispatchReader(
        by=FsKeys.SUFFIX,
        routes={
            "md": kbdoc_reader,
            "html": html_reader,
            "htm": html_reader,
        },
        reader_id=_DISPATCH_READER_ID,
    )
