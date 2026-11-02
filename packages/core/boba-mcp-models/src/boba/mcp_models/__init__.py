"""Чистые карточки MCP и пометки доступа без SDK и инфраструктуры.

Исполнители используют ToolHints, агент и адаптеры — ToolCard и Audience.
Преобразования wire-моделей SDK находятся в boba.mcp_models.protocol.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = ["Audience", "ToolCard", "ToolHints"]


class Audience(StrEnum):
    """Кому адресован блок или ресурс: роли annotations.audience стандарта
    MCP; без audience блок видят обе стороны."""

    MODEL = "assistant"
    USER = "user"

    def reads(self, audience: Sequence[str] | None) -> bool:
        if audience is None:
            return True

        return self.value in audience


@dataclass(frozen=True)
class ToolHints:
    """Пометки инструмента по стандартным annotations MCP: читает ли он
    только (readOnlyHint) и может ли разрушить данные (destructiveHint).

    По ним разрешения решают, спрашивать ли подтверждение перед вызовом.
    Инструмент без пометок считается меняющим и разрушающим — так же
    трактует отсутствие annotations стандарт MCP.
    """

    read_only: bool = False
    destructive: bool = True


@dataclass(frozen=True)
class ToolCard:
    """Карточка инструмента сервера для модели: полное имя, описание,
    JSON-схема аргументов и пометки."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    hints: ToolHints
