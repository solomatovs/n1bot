"""Отказы работы с соединениями субъекта: их kind уходит в чат и в историю.

Вид соединения инструмент объявляет типом параметра (маркер UserConnection),
подпись клиента и строку журнала пишет сам профиль.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ConnectionRefusal"]


class ConnectionRefusal(StrEnum):
    """Причины отказа в доступе к профилю или credentials пользователя."""

    AMBIGUOUS = "ambiguous_connection"
    NO_DELEGATION = "no_delegated_credentials"
    NOT_VISIBLE = "connection_not_visible"
