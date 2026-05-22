"""DI-провайдеры KB-плагина — только stateless reader'ы.

Reader'ы — простые singleton'ы без конфига; шарятся между ingest-tools
(`confluence_*_ingest`) и CLI-runner'ами (`cli/kbdoc_ingest`) через DI.
Они не зависят от подключений или схемы.

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

from boba.kbdoc import KbDocReader
from boba.tools import Scope, provides

__all__ = [
    "provide_kbdoc_reader",
]


@provides(scope=Scope.APP)
def provide_kbdoc_reader() -> KbDocReader:
    """KbDoc-формат (`**key:** value` header + body). Один файл = одна Section."""
    return KbDocReader()
