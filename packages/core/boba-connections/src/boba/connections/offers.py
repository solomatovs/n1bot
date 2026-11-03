"""Схема именованного параметра connection для MCP.

Ошибки: своих не выпускает."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from boba.toolkit.calls import SchemaMark

__all__ = [
    "ConnectionOffer",
    "ConnectionParamSchema",
    "OfferedSource",
]


class OfferedSource(StrEnum):
    """Источник профиля на MCP: каталог PostgreSQL либо сервисный конфиг."""

    SERVER = "server"
    STORED = "stored"


@dataclass(frozen=True)
class ConnectionOffer:
    """Доступные источники параметра: вид, заданные имена и доступ к каталогу MCP."""

    kind: str
    names: Mapping[str, str]
    stored: bool = False


class ConnectionParamText(StrEnum):
    """Описание выбора connection, которое сервер публикует в JSON Schema."""

    SERVER = "Name of the {kind} connection to use. Use one of: {listed}."
    STORED = (
        "Name of the {kind} connection to use. Find available connections "
        "with connection_list or connection_search. The server checks your "
        "grants on every call."
    )

    def render(self, kind: str, listed: str) -> str:
        return self.value.format(kind=kind, listed=listed)


class ConnectionParamSchema:
    """Строит строковую схему имени connection для карточки инструмента."""

    def declared(self, offer: ConnectionOffer) -> dict[str, Any]:
        """Схема имени: вид connection, подсказка каталога и доступные имена конфига."""
        branches: list[dict[str, Any]] = []
        if offer.stored:
            branches.append({"type": "string"})

        if offer.names:
            branches.append(
                {"type": "string", SchemaMark.ENUM.value: list(offer.names)}
            )

        described: list[str] = []
        for name, about in offer.names.items():
            described.append(f"{name} — {about}")

        listed = "; ".join(described)
        text = ConnectionParamText.SERVER
        if offer.stored:
            text = ConnectionParamText.STORED

        declared: dict[str, Any] = {
            SchemaMark.CONNECTION.value: offer.kind,
            SchemaMark.DESCRIPTION.value: text.render(offer.kind, listed),
        }
        if len(branches) > 1:
            return {**declared, SchemaMark.ANY_OF.value: branches}

        return {**declared, **branches[0]}
