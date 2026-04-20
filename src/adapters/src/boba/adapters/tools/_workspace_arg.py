"""Хелперы для tools, привязанных к конкретному workspace.

Workspace кодируется в имени tool'а как префикс с разделителем — ``tmp__ls``,
``user__read_file``. Регистрация создаёт отдельный инстанс tool'а на каждый
разрешённый :class:`WorkspaceKind`, поэтому параметра ``workspace`` в
аргументах tool'ов больше нет.
"""

from __future__ import annotations

from boba.domain.core.tools import ToolId
from boba.domain.core.workspace import WorkspaceKind


def workspace_tool_id(kind: WorkspaceKind, base_name: str) -> ToolId:
    """Собрать :class:`ToolId` вида ``{workspace}__{base_name}``."""
    return ToolId(f"{kind.name}{'__'}{base_name}")
