"""Манифест bash-плагина: фабричный инструмент с исполнителем секции."""

from typing import Final

from boba.tool.shell.tools import BashToolConfig, build_bash_tool
from boba.toolkit.entry import ToolLike
from boba.toolkit.launcher import LauncherFactory
from boba.toolkit.manifest import ToolPluginManifest


class BashPlugin:
    """Сборка bash-инструмента по конфигу секции."""

    @staticmethod
    def build(cfg: BashToolConfig, launchers: LauncherFactory) -> list[ToolLike]:
        return [build_bash_tool(cfg, launchers)]


MANIFEST: Final = ToolPluginManifest(
    section="bash",
    config_model=BashToolConfig,
    build=BashPlugin.build,
)
