"""Подписи панели настроек: строки живут в переводах chainlit развёртывания.

Ключ chat.settings.llm файлов <root>/.chainlit/translations (UiText).

Ошибки:
UiTextError — в переводах нет строки ни на языке пользователя, ни на языке
    по умолчанию.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from boba.aid.chat.ui_text import UiText

__all__ = ["PanelText"]


class PanelText:
    """Строки панели на языке вкладки; без перевода — язык по умолчанию.

    Создаётся на каждую отрисовку панели (AgentPanel) из корня приложения и
    языка сессии chainlit.
    """

    DEFAULT_LANGUAGE: ClassVar[str] = UiText.DEFAULT_LANGUAGE
    SECTION: ClassVar[str] = "chat.settings.llm"

    def __init__(self, root: str, language: str) -> None:
        self._text = UiText(Path(root), language)

    def tab(self, tab_id: str) -> str:
        return self._text.text(f"{self.SECTION}.tabs.{tab_id}")

    def field_label(self, name: str) -> str:
        """Подпись поля панели по его имени (id виджета)."""
        return self._text.text(f"{self.SECTION}.fields.{name}.label")

    def field_description(self, name: str) -> str:
        return self._text.text(f"{self.SECTION}.fields.{name}.description")

    def kept_hint(self) -> str:
        """Подсказка поля ключа: сохранённый ключ есть, пустое поле его оставит."""
        return self._text.text(f"{self.SECTION}.kept")
