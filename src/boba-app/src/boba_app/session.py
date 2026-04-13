"""ChatSession — контекст сессии чата для DI scope.

Не создаёт директории — за это отвечает WorkspaceService.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba_domain.config import AppConfig
from boba_domain.di_types import WorkspaceContext


@dataclass(frozen=True)
class ChatSession:
    """Инициализированная сессия чата."""

    folder: str
    workspace_context: WorkspaceContext

    @staticmethod
    def from_folder(cfg: AppConfig, folder: str) -> ChatSession:
        """Построить контекст сессии из folder ID.

        Предполагает, что workspace уже подготовлен WorkspaceService.
        """
        return ChatSession(
            folder=folder,
            workspace_context=WorkspaceContext(
                folder_path=cfg.workspace_path(folder),
            ),
        )
