"""boba-tool-files — плагин с 16 builtin file-system tools.

Entry-point модуль для `AgentBuilder.use_plugin(boba.tool.files)` или
discovery через `[project.entry-points."boba.plugins"]`.

Все 16 tools шарят `FilesPluginConfig` (`[tool.files]`,
`BOBA_TOOL__FILES__*`). Включаются вместе через `enable=true`; allowlist
`tools` в конфиге выбирает подмножество. `ProjectWorkspaceShell`
инжектится в каждый tool через `FromDI(Scope.APP)` — приложение
обязано зарегистрировать provider'а в `AgentBuilder`.
"""

from __future__ import annotations

from boba.tool.files.append import append
from boba.tool.files.cat import cat
from boba.tool.files.cd import cd
from boba.tool.files.config import FilesPluginConfig
from boba.tool.files.cp import cp
from boba.tool.files.edit import edit
from boba.tool.files.grep import grep
from boba.tool.files.ls import ls
from boba.tool.files.mkdir import mkdir
from boba.tool.files.mv import mv
from boba.tool.files.pwd import pwd
from boba.tool.files.rm import rm
from boba.tool.files.stat import stat
from boba.tool.files.touch import touch
from boba.tool.files.tree import tree
from boba.tool.files.unzip import unzip
from boba.tool.files.write import write

__all__ = [
    "FilesPluginConfig",
    "append",
    "cat",
    "cd",
    "cp",
    "edit",
    "grep",
    "ls",
    "mkdir",
    "mv",
    "pwd",
    "rm",
    "stat",
    "touch",
    "tree",
    "unzip",
    "write",
]
