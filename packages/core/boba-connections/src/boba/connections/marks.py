"""Отказы работы с соединениями субъекта: их kind уходит в чат и в историю.

Вид соединения инструмент объявляет типом параметра (маркер UserConnection),
подпись клиента и строку журнала пишет сам профиль.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ConnectionRefusal"]


class ConnectionRefusal(StrEnum):
    """Отказы работы с соединениями субъекта: whitelist на вызов и правка строк."""

    AMBIGUOUS = "ambiguous_connection"
    NO_DELEGATION = "no_delegated_credentials"
    HOST_NOT_ALLOWED = "host_not_allowed"
    NOT_VISIBLE = "connection_not_visible"
    NOT_OWNED = "connection_not_owned"
    NAME_TAKEN = "connection_name_taken"
    IN_USE = "connection_in_use"
