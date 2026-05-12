"""FS-реализация WorkspaceCatalog: scandir поверх base_dir registry."""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from boba.workspace.catalog import WorkspaceCatalog, WorkspaceSummary
from boba.workspace.contract import WorkspaceId


class FsWorkspaceCatalog(WorkspaceCatalog):
    """Перечисляет директории под base_dir и парсит имена как WorkspaceId."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def iterator(self) -> Iterable[WorkspaceSummary]:
        if not self._base_dir.is_dir():
            return

        for entry in os.scandir(self._base_dir):
            if not entry.is_dir():
                continue
            try:
                workspace_id = WorkspaceId.from_wire(entry.name)
            except ValueError:
                continue
            mtime = datetime.fromtimestamp(entry.stat().st_mtime)

            yield WorkspaceSummary(workspace_id=workspace_id, last_used_at=mtime)
