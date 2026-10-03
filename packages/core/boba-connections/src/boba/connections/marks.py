"""Отказы работы с соединениями субъекта: их kind уходит в чат и в историю.

Вид соединения инструмент объявляет типом параметра (маркер UserConnection),
подпись клиента и строку журнала пишет сам профиль.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ConnectionRefusal"]


class ConnectionRefusal(StrEnum):
    """Отказы работы с соединениями субъекта: whitelist на вызов, правка строк
    и приём запечатанного соединения исполнителем."""

    AMBIGUOUS = "ambiguous_connection"
    NO_DELEGATION = "no_delegated_credentials"
    NOT_VISIBLE = "connection_not_visible"
    NOT_OWNED = "connection_not_owned"
    NAME_TAKEN = "connection_name_taken"
    IN_USE = "connection_in_use"
    NOT_SEALED = "connection_not_sealed"
    SEAL_KEY_UNKNOWN = "connection_seal_key_unknown"
    SEAL_DAMAGED = "connection_seal_damaged"
    SEAL_EXPIRED = "connection_seal_expired"
    SEALED_FOR_ANOTHER_USER = "connection_sealed_for_another_user"
    ANOTHER_KIND = "connection_of_another_kind"
