"""SettingsSection — модульный раздел шестерёнки.

Каждый раздел сам знает:
  * какие Tab'ы показать (учитывая текущий ThreadMeta);
  * как разобрать значения из словаря, который chainlit отдаёт в
    on_settings_update, и применить их через узкие методы
    ThreadRepository.

Реестр секций задаётся в composition.main() и попадает в AppState;
callbacks.py итерирует его при отрисовке и при сохранении настроек.
Чтобы добавить раздел (модель, sampling, …), реализуй новый класс и
включи его в реестр — общий код callback'ов трогать не надо.
"""

from __future__ import annotations

from typing import Any, Protocol

from boba.chainlit.models import ThreadId, ThreadMeta
from boba.chainlit.storage import ThreadRepository
from chainlit.input_widget import Tab

__all__ = ["SettingsSection"]


class SettingsSection(Protocol):
    """Один раздел настроек чата."""

    def widgets(self, meta: ThreadMeta | None) -> list[Tab]:
        """Tab'ы этой секции под текущую мету.

        Пустой список ⇒ раздел нечего показать (например, catalog tools
        ещё не закеширован) — orchestrator его просто не выводит.
        """
        ...

    async def apply(
        self,
        settings: dict[str, Any],
        thread_id: ThreadId,
        repo: ThreadRepository,
    ) -> None:
        """Применить значения из settings к мете через узкие методы repo."""
        ...
