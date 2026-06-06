"""Раздел «System prompt»."""

from __future__ import annotations

from typing import Any, ClassVar

from boba.chainlit.models import ThreadId, ThreadMeta
from boba.chainlit.storage import ThreadRepository
from boba.chainlit.system_prompt import DefaultSystemPromptSource
from chainlit.input_widget import Tab, TextInput

__all__ = ["PromptSection"]


class PromptSection:
    """Один Tab с многострочным TextInput для пользовательского system-prompt."""

    WIDGET_ID: ClassVar[str] = "system_prompt"
    TAB_ID: ClassVar[str] = "prompt"

    def __init__(self, default_prompt_source: DefaultSystemPromptSource) -> None:
        self._default_prompt_source = default_prompt_source

    def widgets(self, meta: ThreadMeta | None) -> list[Tab]:
        initial = (
            meta.system_prompt
            if meta is not None and meta.system_prompt
            else self._default_prompt_source.read()
        )
        return [
            Tab(
                id=self.TAB_ID,
                label="System prompt",
                inputs=[
                    TextInput(
                        id=self.WIDGET_ID,
                        label="System prompt",
                        initial=initial,
                        multiline=True,
                    ),
                ],
            ),
        ]

    async def apply(
        self,
        settings: dict[str, Any],
        thread_id: ThreadId,
        repo: ThreadRepository,
    ) -> None:
        value = settings.get(self.WIDGET_ID)
        if isinstance(value, str):
            await repo.set_system_prompt(thread_id, value)
