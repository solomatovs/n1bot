"""boba-tool-files — v2 плагин с 15 builtin file-system tools.

Entry-point модуль для `AgentBuilder.use_plugin(boba.tool.files)` или
discovery через `[project.entry-points."boba.plugins"]`.

Все 15 tools шарят `FilesPluginConfig` (`[tool.files]`,
`BOBA_TOOL__FILES__*`). Включаются вместе через `enable=true`; allowlist
`tools` в конфиге выбирает подмножество. `ProjectWorkspaceShell`
инжектится в каждый tool через `FromDI(Scope.APP)` — приложение
обязано зарегистрировать provider'а в `AgentBuilder`.
"""

from __future__ import annotations

from boba.tool.files.append import AppendTool
from boba.tool.files.cat import CatTool
from boba.tool.files.cd import CdTool
from boba.tool.files.config import FilesPluginConfig
from boba.tool.files.cp import CpTool
from boba.tool.files.edit import EditTool
from boba.tool.files.grep import GrepTool
from boba.tool.files.ls import LsTool
from boba.tool.files.mkdir import MkdirTool
from boba.tool.files.mv import MvTool
from boba.tool.files.pwd import PwdTool
from boba.tool.files.rm import RmTool
from boba.tool.files.stat import StatTool
from boba.tool.files.touch import TouchTool
from boba.tool.files.tree import TreeTool
from boba.tool.files.write import WriteTool

__all__ = [
    "AppendTool",
    "CatTool",
    "CdTool",
    "CpTool",
    "EditTool",
    "FilesPluginConfig",
    "GrepTool",
    "LsTool",
    "MkdirTool",
    "MvTool",
    "PwdTool",
    "RmTool",
    "StatTool",
    "TouchTool",
    "TreeTool",
    "WriteTool",
]
