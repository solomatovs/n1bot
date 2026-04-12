"""Workspace — рабочая папка с документами.

Описывает что лежит на диске: путь к папке, путь к манифесту, путь к истории.
Не содержит инфраструктурных сервисов и конфигурации приложения.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Workspace:
    """Рабочая папка с документами."""

    folder_path: Path
    boba_path: Path
    manifest_path: Path
    history_path: Path

    def source_file_path(self, filename: str) -> Path:
        """Полный путь к файлу в папке."""
        return self.folder_path / filename
