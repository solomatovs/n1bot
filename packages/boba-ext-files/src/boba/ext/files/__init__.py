"""Boba extension: builtin file-system tools.

Регистрируется в боба через entry-point ``boba.tools`` (см. pyproject.toml).
ToolPluginLoader при старте процесса вызывает :func:`register_tools` —
функция возвращает один :class:`StaticToolSource` со всеми 15 файловыми
tools (cat/ls/grep/edit/write/...) под общим ``ToolSourceId("builtin.files")``.

Зависимостей кроме ``boba`` пакет не имеет: все tools работают через
:class:`~boba.domain.core.tools.ToolContext.project_workspace`.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.domain.core.tools import StaticToolSource, ToolSource, ToolSourceId
from boba.ext.files.append import AppendTool
from boba.ext.files.cat import CatTool
from boba.ext.files.cd import CdTool
from boba.ext.files.cp import CpTool
from boba.ext.files.edit import EditTool
from boba.ext.files.grep import GrepTool
from boba.ext.files.ls import LsTool
from boba.ext.files.mkdir import MkdirTool
from boba.ext.files.mv import MvTool
from boba.ext.files.pwd import PwdTool
from boba.ext.files.rm import RmTool
from boba.ext.files.stat import StatTool
from boba.ext.files.touch import TouchTool
from boba.ext.files.tree import TreeTool
from boba.ext.files.write import WriteTool
from boba.infra.tool_plugin_loader import ExtensionContext

__all__ = ["register_tools"]


def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    """Entry-point ``boba.tools``: возвращает все файловые tools одним
    источником.

    ``ctx`` пока не используется — пакет не имеет конфига и не нуждается
    в зависимостях из :class:`~boba.infra.config.ConfigBundle`. Параметр
    сохранён для совместимости с :data:`RegisterToolsFn`.
    """
    del ctx
    yield StaticToolSource(
        ToolSourceId("builtin.files"),
        priority=0,
        tools=[
            AppendTool(),
            CatTool(),
            CdTool(),
            CpTool(),
            EditTool(),
            GrepTool(),
            LsTool(),
            MkdirTool(),
            MvTool(),
            PwdTool(),
            RmTool(),
            StatTool(),
            TouchTool(),
            TreeTool(),
            WriteTool(),
        ],
    )
