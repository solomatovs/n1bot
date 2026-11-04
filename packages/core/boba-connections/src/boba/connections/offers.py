"""Схема именованного параметра connection для MCP.

Ошибки: своих не выпускает."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from boba.toolkit.calls import SchemaMark

__all__ = [
    "ConnectionOffer",
    "ConnectionParamSchema",
]


@dataclass(frozen=True)
class ConnectionOffer:
    """Вид соединения для строковой схемы MCP."""

    kind: str


class ConnectionParamText(StrEnum):
    """Описание выбора connection, которое сервер публикует в JSON Schema."""

    STORED = (
        "Name of the {kind} connection to use. Find available connections "
        "with connection_list or connection_search. The server checks your "
        "grants on every call."
    )

    def render(self, kind: str) -> str:
        return self.value.format(kind=kind)


class ConnectionParamSchema:
    """Строит строковую схему имени connection для карточки инструмента."""

    def declared(self, offer: ConnectionOffer) -> dict[str, Any]:
        """Схема строкового имени и подсказка, как найти доступный профиль."""
        return {
            SchemaMark.CONNECTION.value: offer.kind,
            SchemaMark.DESCRIPTION.value: ConnectionParamText.STORED.render(offer.kind),
            "type": "string",
        }
