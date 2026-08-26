"""Плагины инструментов чата: таблица секций [tool.<name>] и сборка реестра."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.tools import BaseTool
from omegaconf import DictConfig

from boba.canvas.diagram import DiagramToolConfig
from boba.chainlit.agent.tools.send_file import build_send_file_tool
from boba.chainlit.canvas.diagram import build_diagram_tools
from boba.chainlit.canvas.stream_logs import build_stream_logs_tools
from boba.chainlit.canvas.tools import CanvasToolConfig, build_canvas_tools
from boba.chainlit.infra.kerberos_refresh import ChatRefreshSignal
from boba.connection_broker.user_connections import RegistryRef, StoreRef
from boba.connections.marks import UserConnectionsSpec
from boba.connections.profile import ConnectionKind
from boba.connections.whitelist import ConnectionKeying
from boba.runtime.plugins import ToolBridge, ToolLoader, ToolPlugin
from boba.tool.ch.tools import TOOLS as CH_TOOLS
from boba.tool.chart.tools import TOOLS as CHART_TOOLS
from boba.tool.doc.tools import TOOLS as DOC_TOOLS
from boba.tool.kb.confluence.ingest_tools import TOOLS as INGEST_TOOLS
from boba.tool.kb.confluence.tools import TOOLS as CONFLUENCE_TOOLS
from boba.tool.kb.tools import TOOLS as KB_TOOLS
from boba.tool.pg.tools import TOOLS as PG_TOOLS
from boba.tool.shell.tools import BashToolConfig, build_bash_tool
from boba.tool.web.tools import TOOLS as WEB_TOOLS
from boba.toolkit.entry import ToolLike
from boba.toolkit.launcher import LauncherFactory
from boba.toolrun.registry import ToolRegistry
from boba.workflow_engine.tools import WorkflowToolConfig, build_workflow_tools

__all__ = ["ChatPlugins"]


class ChatPlugins:
    """Таблица плагинов приложения чата и загрузка реестра инструментов."""

    @classmethod
    def load(
        cls, raw_config: DictConfig, store_ref: StoreRef, registry_ref: RegistryRef
    ) -> ToolRegistry:
        loader = ToolLoader(
            raw_config, cls.table(), store_ref, registry_ref, ChatRefreshSignal()
        )

        return loader.load()

    @classmethod
    def table(cls) -> Mapping[str, ToolPlugin]:
        return {
            "bash": ToolPlugin(
                section="bash",
                config_model=BashToolConfig,
                build=cls._bash,
            ),
            "doc": cls._module("doc", DOC_TOOLS),
            "chart": cls._module("chart", CHART_TOOLS),
            "send_file": ToolPlugin(
                section="send_file",
                chat_only=True,
                build=cls._send_file,
                sandboxed=False,
            ),
            "diagram": ToolPlugin(
                section="diagram",
                chat_only=True,
                config_model=DiagramToolConfig,
                build=cls._diagram,
                sandboxed=False,
            ),
            "canvas": ToolPlugin(
                section="canvas",
                chat_only=True,
                config_model=CanvasToolConfig,
                build=cls._canvas,
                sandboxed=False,
            ),
            "stream_logs": ToolPlugin(
                section="stream_logs",
                build=cls._stream_logs,
                sandboxed=False,
            ),
            "workflow": ToolPlugin(
                section="workflow",
                config_model=WorkflowToolConfig,
                build=cls._workflow,
                sandboxed=False,
            ),
            "pg": cls._connected(
                "pg", PG_TOOLS, ConnectionKind.POSTGRES, ConnectionKeying.NAME
            ),
            "ch": cls._connected(
                "ch", CH_TOOLS, ConnectionKind.CLICKHOUSE, ConnectionKeying.NAME
            ),
            "kb": cls._module("kb", KB_TOOLS),
            "confluence": cls._module("confluence", CONFLUENCE_TOOLS),
            "ingest": cls._module("ingest", INGEST_TOOLS),
            "web": cls._connected(
                "web", WEB_TOOLS, ConnectionKind.WEB, ConnectionKeying.NAME
            ),
        }

    @staticmethod
    def _module(section: str, tools: Sequence[ToolLike]) -> ToolPlugin:
        return ToolPlugin(
            section=section,
            module_tools=ToolBridge.toolset(tools),
            modules=ToolBridge.modules_of(tools),
        )

    @staticmethod
    def _connected(
        section: str,
        tools: Sequence[ToolLike],
        kind: ConnectionKind,
        keying: ConnectionKeying,
    ) -> ToolPlugin:
        return ToolPlugin(
            section=section,
            module_tools=ToolBridge.toolset(tools),
            modules=ToolBridge.modules_of(tools),
            connections=UserConnectionsSpec(kind, keying),
        )

    @staticmethod
    def _bash(cfg: BashToolConfig, launchers: LauncherFactory) -> list[BaseTool]:
        return [ToolBridge.as_structured_tool(build_bash_tool(cfg, launchers))]

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

    @staticmethod
    def _workflow(
        cfg: WorkflowToolConfig, launchers: LauncherFactory
    ) -> list[BaseTool]:
        """Сервис берётся из контейнера на вызов: providers импортируют этот модуль."""
        from boba.chainlit.infra.providers import workflow_service_ref  # noqa: PLC0415

        return build_workflow_tools(cfg, workflow_service_ref)
