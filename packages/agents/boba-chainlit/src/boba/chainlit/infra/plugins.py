"""Плагины инструментов чата: таблица секций [tool.<name>] и сборка реестра."""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.tools import BaseTool
from omegaconf import DictConfig

from boba.access import GrantCheck
from boba.canvas.diagram import DiagramToolConfig
from boba.chainlit.agent.tools.send_file import build_send_file_tool
from boba.chainlit.canvas.diagram import build_diagram_tools
from boba.chainlit.canvas.stream_logs import build_stream_logs_tools
from boba.chainlit.canvas.tools import CanvasToolConfig, build_canvas_tools
from boba.runtime.plugins import CoreTools, ToolLoader, ToolPlugin
from boba.runtime.refs import RuntimeRefs
from boba.toolkit.launcher import LauncherFactory
from boba.toolrun.registry import ToolRegistry

__all__ = ["ChatPlugins"]


class ChatPlugins:
    """Таблица плагинов приложения чата и загрузка реестра инструментов."""

    @classmethod
    def load(cls, raw_config: DictConfig, refs: RuntimeRefs) -> ToolRegistry:
        loader = ToolLoader(
            raw_config,
            cls.table(refs),
            refs,
            GrantCheck.STRICT,
        )

        return loader.load()

    @classmethod
    def table(cls, refs: RuntimeRefs) -> Mapping[str, ToolPlugin]:
        """Общая таблица процессов плюс инструменты, живущие только в чате."""
        table: dict[str, ToolPlugin] = dict(CoreTools.table(refs))
        table["send_file"] = ToolPlugin(
            section="send_file",
            chat_only=True,
            build=cls._send_file,
            sandboxed=False,
        )
        table["diagram"] = ToolPlugin(
            section="diagram",
            chat_only=True,
            config_model=DiagramToolConfig,
            build=cls._diagram,
            sandboxed=False,
        )
        table["canvas"] = ToolPlugin(
            section="canvas",
            chat_only=True,
            config_model=CanvasToolConfig,
            build=cls._canvas,
            sandboxed=False,
        )
        table["stream_logs"] = ToolPlugin(
            section="stream_logs",
            build=cls._stream_logs,
            sandboxed=False,
        )

        return table

    @staticmethod
    def _send_file(cfg: None, launchers: LauncherFactory) -> list[BaseTool]:
        return [build_send_file_tool()]

    @staticmethod
    def _diagram(cfg: DiagramToolConfig, launchers: LauncherFactory) -> list[BaseTool]:
        return build_diagram_tools(cfg)

    @staticmethod
    def _canvas(cfg: CanvasToolConfig, launchers: LauncherFactory) -> list[BaseTool]:
        return build_canvas_tools(cfg)

    @staticmethod
    def _stream_logs(cfg: None, launchers: LauncherFactory) -> list[BaseTool]:
        return build_stream_logs_tools(cfg)
