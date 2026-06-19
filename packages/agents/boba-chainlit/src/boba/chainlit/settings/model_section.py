"""Раздел «System prompt»."""

from __future__ import annotations

from typing import Any, ClassVar

from boba.chainlit.models import ThreadId, ThreadMeta
from boba.chainlit.storage import ThreadRepository
from chainlit.input_widget import Select, Tab

__all__ = ["ModelSection"]


class ModelSection:
    """Один Tab с многострочным TextInput для пользовательского system-prompt."""

    WIDGET_ID: ClassVar[str] = "model_widget"
    TAB_ID: ClassVar[str] = "models"

    def __init__(self, model_avaliable: list[str]) -> None:
        self._model_avaliable = model_avaliable

    def widgets(self, meta: ThreadMeta | None) -> list[Tab]:
        initial_index: int = 0

        if meta and meta.model:
            initial_index = self._model_avaliable.index(meta.model)

        return [
            Tab(
                id=self.TAB_ID,
                label="Models",
                inputs=[
                    Select(
                        id=self.WIDGET_ID,
                        label="Models",
                        initial_index=initial_index,
                        values=self._model_avaliable,
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
        model = settings.get(self.WIDGET_ID)
        if isinstance(model, str):
            await repo.set_model(thread_id, model)
