"""ChatSession — единая точка инициализации сессии чата.

Создаёт рабочую структуру директорий, генерирует chat_id,
возвращает готовый FolderContext для DI scope.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from boba_domain.config import AppConfig
from boba_domain.di_types import FolderContext


@dataclass(frozen=True)
class ChatSession:
    """Инициализированная сессия чата."""

    chat_id: str
    folder_path: Path
    folder_context: FolderContext

    @staticmethod
    def create(
        cfg: AppConfig,
        folder_name: str,
        chat_id: str | None = None,
    ) -> ChatSession:
        """Создать сессию: директории, chat_id, history файл."""
        folder_path = Path(cfg.import_base_dir) / folder_name
        folder_path.mkdir(parents=True, exist_ok=True)
        cfg.boba_path(folder_path).mkdir(parents=True, exist_ok=True)

        resolved_id = chat_id or uuid.uuid4().hex[:12]
        history_path = cfg.workspace_history_path(folder_path)
        history_path.touch(exist_ok=True)

        return ChatSession(
            chat_id=resolved_id,
            folder_path=folder_path,
            folder_context=FolderContext(
                folder_path=folder_path,
                history_path=history_path,
            ),
        )
