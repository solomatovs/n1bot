"""Доменные ошибки Confluence-инструмента."""

from __future__ import annotations

__all__ = ["ConfluencePayloadError"]


class ConfluencePayloadError(Exception):
    """Невалидный/нечитаемый JSON-payload от Confluence REST.

    Поднимается decoder'ами и reader'ами при ошибке разбора ответа.
    """
