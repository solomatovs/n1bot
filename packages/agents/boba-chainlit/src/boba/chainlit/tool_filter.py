"""Per-thread фильтр над `ToolCatalog`.

`ThreadFilteredToolCatalog` оборачивает обычный catalog и на каждом turn'е
читает свежий `ThreadMeta.enabled_tool_ids` из репозитория:
- `None` ⇒ отдаёт всё, что есть в inner (backward-compat для тредов без выбора).
- `list` ⇒ оставляет только tool_id, попадающие в список; неизвестные молча
  игнорируются (валидация — на стороне chainlit при сохранении настроек).

Catalog filtration — единственный механизм; `ToolExecutor` намеренно не
оборачивается: agent выполняет то, что попросила модель, а модель видит
только отфильтрованный subset (см. обсуждение архитектуры).
"""

from __future__ import annotations

from collections.abc import Iterator

from boba.chainlit.models import ThreadId
from boba.chainlit.storage import ThreadRepository
from boba.tools.domain.tool import ToolSchema
from boba.tools.framework import ToolCatalog

__all__ = ["ThreadFilteredToolCatalog"]


class ThreadFilteredToolCatalog(ToolCatalog):
    """Catalog-view, отдающий только tool'ы, разрешённые для thread'а."""

    def __init__(
        self,
        inner: ToolCatalog,
        repository: ThreadRepository,
        thread_id: ThreadId,
    ) -> None:
        # super-init ставит _sources={} — мы им не пользуемся, переопределяем
        # только definitions(). Передаём пустую mapping, чтобы не падать при
        # потенциальных будущих обращениях к _sources внутри ToolCatalog.
        super().__init__({})
        self._inner = inner
        self._repository = repository
        self._thread_id = thread_id

    def definitions(self) -> Iterator[ToolSchema]:
        allowed = self._allowed_set()
        for schema in self._inner.definitions():
            if allowed is None or schema.name in allowed:
                yield schema

    def _allowed_set(self) -> set[str] | None:
        meta = self._repository.get_meta_sync(self._thread_id)
        if meta is None or meta.enabled_tool_ids is None:
            return None
        return set(meta.enabled_tool_ids)
