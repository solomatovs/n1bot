"""Основание домена каталога: базовая модель и ошибки, общие для процесса и
источников.

Ошибки:
CatalogError — базовая ошибка домена.
CatalogInvariantError — снимок нарушает инварианты, перечень в violations.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = ["CatalogError", "CatalogInvariantError", "CatalogModel", "ChangeStatus"]


class ChangeStatus(StrEnum):
    """Статус сущности или объекта относительно другой версии."""

    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    UNCHANGED = "unchanged"


class CatalogError(Exception):
    """Базовая ошибка домена; наследники — CatalogInvariantError и ошибки операций."""


class CatalogInvariantError(CatalogError):
    """Снимок нарушает инварианты; каждое нарушение отдельной строкой."""

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = tuple(violations)
        text = "; ".join(self.violations)
        super().__init__(text)


class CatalogModel(BaseModel):
    """Базовая модель домена: неизменяемая, лишние ключи запрещены."""

    model_config = ConfigDict(frozen=True, extra="forbid")
