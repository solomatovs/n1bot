"""Helper для tools, привязанных к конкретному workspace.

Workspace кодируется в имени tool'а как префикс с разделителем
(``user__ls``, ``tmp__read_file``). Регистрация создаёт отдельный
инстанс tool'а на каждый разрешённый :class:`WorkspaceKind`.
"""

from __future__ import annotations

from boba.domain.core.tools import ToolId
from boba.domain.core.workspace import WorkspaceKind


class WorkspaceScopedToolId:
    """Конструктор :class:`ToolId` с префиксом workspace'а.

    Имя tool'а собирается как ``{workspace}{SEPARATOR}{base_name}``,
    например ``user__ls``. Класс — namespace для разделителя и фабрики;
    сам он не инстанцируется.
    """

    SEPARATOR = "__"

    @classmethod
    def build(cls, kind: WorkspaceKind, base_name: str) -> ToolId:
        return ToolId(f"{kind.name}{cls.SEPARATOR}{base_name}")
