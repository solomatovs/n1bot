"""WorkspaceCatalog: enumeration существующих workspace'ов."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from boba.workspace.contract import WorkspaceId

__all__ = ["WorkspaceCatalog", "WorkspaceSummary"]


@dataclass(frozen=True)
class WorkspaceSummary:
    """Минимальная метаинформация о workspace'е для каталога."""

    workspace_id: WorkspaceId
    last_used_at: datetime


class WorkspaceCatalog(ABC):
    """Источник списка существующих workspace'ов."""

    @abstractmethod
    def iterator(self) -> Iterable[WorkspaceSummary]:
        """Все известные workspace'ы; порядок определяет реализация."""
        ...
