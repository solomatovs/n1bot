"""ChatSession — единая точка инициализации сессии чата.

Создаёт рабочую структуру директорий,
возвращает готовый FolderContext для DI scope.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba_domain.config import AppConfig
from boba_domain.di_types import WorkspaceContext


@dataclass(frozen=True)
class ChatSession:
    """Инициализированная сессия чата."""

    id: str  # thread_id = folder name
    workspace_context: WorkspaceContext

    @staticmethod
    def create(cfg: AppConfig, id: str) -> ChatSession:
        """Создать сессию. chat_id = thread_id = имя папки workspace."""
        workspace_path = cfg.workspace_path(id)
        workspace_path.mkdir(parents=True, exist_ok=True)
        cfg.boba_path(workspace_path).mkdir(parents=True, exist_ok=True)
        cfg.workspace_history_path(workspace_path).touch(exist_ok=True)

        return ChatSession(
            id=id,
            workspace_context=WorkspaceContext(folder_path=workspace_path),
        )
