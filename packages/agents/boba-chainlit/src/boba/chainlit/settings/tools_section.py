"""Раздел «Tools»: по табу на ToolSource, чекбоксы внутри."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, ClassVar

from boba.chainlit.models import ThreadId, ThreadMeta
from boba.chainlit.storage import ThreadRepository
from boba.chainlit.tool_cache import AvailableToolsCache
from boba.tools.domain.tool import ToolSchema
from chainlit.input_widget import Checkbox, InputWidget, Tab

__all__ = ["ToolsSection"]


class ToolsSection:
    """Tab per `source_id`; внутри по чекбоксу на каждый tool этого source.

    Если catalog ещё не закеширован (первый `on_chat_start` до build'а
    ChatSession) — секция возвращает `[]` и orchestrator её просто скрывает.
    Whitelist хранится в `ThreadMeta.enabled_tool_ids`: `None` ⇒ все включены,
    список ⇒ ровно эти. Неизвестные id молча отбрасываются при apply.
    """

    WIDGET_PREFIX: ClassVar[str] = "tool:"
    TAB_ID_PREFIX: ClassVar[str] = "tools:"

    def __init__(self, available_tools: AvailableToolsCache) -> None:
        self._available_tools = available_tools

    def widgets(self, meta: ThreadMeta | None) -> list[Tab]:
        schemas = self._available_tools.schemas()
        if not schemas:
            return []
        enabled = self._enabled_set(meta, schemas)
        by_source: dict[str, list[ToolSchema]] = defaultdict(list)
        for schema in schemas:
            by_source[schema.source_id].append(schema)
        tabs: list[Tab] = []
        for source_id in sorted(by_source):
            # Явный `list[InputWidget]`: chainlit.Tab.inputs инвариантен,
            # без аннотации pyright не приводит list[Checkbox] к list[InputWidget].
            inputs: list[InputWidget] = [
                Checkbox(
                    id=f"{self.WIDGET_PREFIX}{schema.name}",
                    label=schema.name,
                    initial=schema.name in enabled,
                    description=schema.description or None,
                )
                for schema in by_source[source_id]
            ]
            tabs.append(
                Tab(
                    id=f"{self.TAB_ID_PREFIX}{source_id}",
                    label=source_id,
                    inputs=inputs,
                ),
            )
        return tabs

    async def apply(
        self,
        settings: dict[str, Any],
        thread_id: ThreadId,
        repo: ThreadRepository,
    ) -> None:
        schemas = self._available_tools.schemas()
        if not schemas:
            # Catalog ещё не известен — не трогаем существующее значение.
            return
        known = {s.name for s in schemas}
        selected = [
            key[len(self.WIDGET_PREFIX) :]
            for key, value in settings.items()
            if key.startswith(self.WIDGET_PREFIX) and value
        ]
        valid = [tool_id for tool_id in selected if tool_id in known]
        await repo.set_enabled_tools(thread_id, valid)

    @staticmethod
    def _enabled_set(
        meta: ThreadMeta | None,
        schemas: tuple[ToolSchema, ...],
    ) -> set[str]:
        # None ⇒ дефолт: все доступные tool'ы отмечены.
        if meta is None or meta.enabled_tool_ids is None:
            return {s.name for s in schemas}
        return set(meta.enabled_tool_ids)
