"""Протокол рендерера истории чата по документам."""
from __future__ import annotations

from typing import Any, List, Protocol, runtime_checkable

from domain.doc_chat import BlockType, DocChatExchange, HistoryBlock


@runtime_checkable
class ChatRenderer(Protocol):
    """Рендерер истории чата — отображает блоки пользователю.

    Все отображение (replay и live-стриминг) проходит через рендерер.
    """

    def render_history(self, exchanges: List[DocChatExchange]) -> None:
        """Отрисовать всю историю обменов."""
        ...

    def render_block(self, block: HistoryBlock) -> None:
        """Отрисовать один завершённый блок."""
        ...

    def render_streaming(
        self,
        placeholder: Any,
        block_type: BlockType,
        text: str,
    ) -> None:
        """Отрисовать стриминговый контент в placeholder (с курсором ▌)."""
        ...
