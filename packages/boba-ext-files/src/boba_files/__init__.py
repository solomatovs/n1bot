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

from boba.adapters.tool_providers import StaticToolSource
from boba.domain.core.tools import ToolSource, ToolSourceId
from boba.infra.tool_plugin_loader import ExtensionContext
from boba_files.append import AppendTool
from boba_files.cat import CatTool
from boba_files.cd import CdTool
from boba_files.cp import CpTool
from boba_files.edit import EditTool
from boba_files.grep import GrepTool
from boba_files.ls import LsTool
from boba_files.mkdir import MkdirTool
from boba_files.mv import MvTool
from boba_files.pwd import PwdTool
from boba_files.rm import RmTool
from boba_files.stat import StatTool
from boba_files.touch import TouchTool
from boba_files.tree import TreeTool
from boba_files.write import WriteTool

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
