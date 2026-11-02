"""Условия отбора соединений, доступных субъекту.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ConnectionFilter"]


@dataclass(frozen=True, kw_only=True)
class ConnectionFilter:
    """Фильтр каталога соединений; SQL строит ConnectionStore в брокере."""

    kind: str = ""
    name: str = ""
    host: str = ""
    description: str = ""
    unique_only: bool = False

    @property
    def empty(self) -> bool:
        return self == ConnectionFilter()
