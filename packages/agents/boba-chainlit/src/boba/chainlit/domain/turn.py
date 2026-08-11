"""Порт идущего хода: инструменты цепляют вложения, не зная про сам ход.

Реализацию регистрирует слой чата на старте приложения — иначе инструменты
зависели бы от оркестрации хода.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar, Protocol

__all__ = ["ActiveTurns", "TurnPort"]


class TurnPort(Protocol):
    """Что инструменту нужно от хода: куда крепить созданный им элемент."""

    @property
    def answer_step_id(self) -> str | None: ...


class ActiveTurns:
    """Реестр идущих ходов: одна точка доступа для инструментов."""

    _RESOLVER: ClassVar[Callable[[str], TurnPort | None] | None] = None

    @classmethod
    def configure(cls, resolver: Callable[[str], TurnPort | None]) -> None:
        cls._RESOLVER = resolver

    @classmethod
    def of(cls, thread_id: str) -> TurnPort | None:
        """Живой ход треда; None — тред ничем не занят или реестр не настроен."""
        resolver = cls._RESOLVER
        if resolver is None:
            return None

        return resolver(thread_id)

    @classmethod
    def reset(cls) -> None:
        """Сброс: пользуются тесты, приложению это не нужно."""
        cls._RESOLVER = None
