"""Built-in плагин: файловые tool'ы для агента.

Образец контракта плагин-системы. Положи этот .py (или его копию,
собранную CI) в директорию ``BOBA_PLUGINS_DIR`` — :class:`PluginLoader`
на старте процесса задискаверит его и зарегистрирует все возвращённые
из ``register(ctx)`` :class:`ToolSource`-ы.

Плагин получает :class:`PluginContext` с per-request
:class:`ProjectWorkspaceShell` (для I/O в проект пользователя) и
application-level singleton'ами (config'и, plugin-workspace).
"""

from collections.abc import Iterable

from boba.adapters.tool_providers import StaticToolSource
from boba.adapters.tools.cat import CatTool
from boba.adapters.tools.ls import LsTool
from boba.domain.core.tools import ToolSource, ToolSourceId
from boba.infra.plugins import PluginContext


def register(ctx: PluginContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files"),
        priority=0,
        tools=[
            CatTool(ctx.project_workspace),
            LsTool(ctx.project_workspace),
        ],
    )
