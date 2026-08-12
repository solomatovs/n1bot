"""Реестр tool-плагинов: секция [tool.<name>] -> langchain-инструменты и узлы стадий.

Плагин отдаёт два состава: фасады для LLM и узлы реестра стадий с профилем
песочницы своей секции. Реестр стадий один на приложение, поэтому исполнитель
(SandboxCaller) тоже один: профиль запуска живёт в узле, а не в фабрике.

Ошибки: RuntimeError — сборка нарушила собственный инвариант (узел плагина
отсутствует, права проверены до сборки карты); ошибки разбора конфига идут из
boba.settings.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from langchain_core.tools import BaseTool
from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict

from boba.chainlit.agent.tools.access import ToolAccess, ToolAccessGuard
from boba.chainlit.agent.tools.cancellation import CancellableTools
from boba.chainlit.agent.tools.canvas import CanvasToolConfig
from boba.chainlit.agent.tools.diagram import DiagramToolConfig
from boba.chainlit.agent.tools.errors import ToolErrorGuard
from boba.chainlit.agent.tools.run_log import ToolRunLogger
from boba.chainlit.domain.session import (
    current_thread_id,
    current_user_id,
    current_user_roles,
)
from boba.chainlit.rendering.stream_view import StageTools
from boba.sandbox import SandboxCaller, SandboxToolConfig, has_bwrap
from boba.sandbox.journal import StreamJournalHub
from boba.sandbox.profile import SandboxProfile
from boba.sandbox.workflow import StageDef, StageRegistry
from boba.settings import bind
from boba.tool.ch import ChExecutorConfig, ChStages, build_ch_tools
from boba.tool.chart import ChartCaller, build_chart_tools
from boba.tool.doc import DocEngine, DocToolsConfig, build_doc_tools
from boba.tool.kb import (
    KbStages,
    PostgresKnowledgeBaseConfig,
    build_kb_tools,
)
from boba.tool.kb.confluence import (
    ConfluenceToolsConfig,
    build_confluence_tools,
)
from boba.tool.kb.confluence.ingest_base import ConfluenceIngestConfig
from boba.tool.kb.confluence.ingest_stages import ConfluenceIngestStages
from boba.tool.kb.confluence.ingest_tools import (
    build_confluence_ingest_tools,
)
from boba.tool.kb.confluence.stages import ConfluenceStages
from boba.tool.kb.html.stages import HtmlStages
from boba.tool.pg import PgExecutorConfig, PgStages, build_pg_tools
from boba.tool.shell import BashStage, build_bash_tool
from boba.tool.web import WebGrepConfig, WebStages, build_web_tools
from boba.toolkit.launcher import LauncherFactory, ToolLauncher
from boba.toolkit.types import StringList
from boba.toolkit.workflow import StageContract, StageNode

__all__ = [
    "PluginBuild",
    "PluginMeta",
    "ToolPlugin",
    "ToolPlugins",
    "ToolRegistry",
    "ToolSection",
    "ToolSections",
    "load_tools",
]

logger = logging.getLogger(__name__)

ConfigT = Any


@dataclass(frozen=True)
class PluginBuild:
    """Вход сборки инструментов плагина: конфиг секции, порт запуска и узлы."""

    cfg: ConfigT
    launchers: LauncherFactory
    nodes: Mapping[str, StageDef]

    def profile_of(self, node: str) -> SandboxProfile:
        """Профиль песочницы узла плагина; чужой узел — нарушение инварианта."""
        definition = self.nodes.get(node)
        if definition is None:
            msg = f"plugin build has no stage node {node!r}"
            raise RuntimeError(msg)

        return definition.profile


@dataclass(frozen=True)
class ToolPlugin:
    """Один tool-плагин: как собрать инструменты и узлы секции [tool.<name>]."""

    section: str
    build: Callable[[PluginBuild], list[BaseTool]]
    nodes: Callable[[ConfigT], Mapping[str, StageNode]]
    config_model: type[BaseModel] | None = None
    sandboxed: bool = True
    """False — инструменты плагина ничего не запускают, секции sandbox у него нет."""


class PluginMeta(BaseModel):
    """Meta-конфиг плагина: что framework читает из [tool.<name>]."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    roles: StringList = []
    tools: dict[str, StringList] = {}

    def roles_of(self, tool_name: str) -> list[str]:
        return self.tools.get(tool_name) or self.roles


def _no_nodes(cfg: ConfigT) -> Mapping[str, StageNode]:
    """Плагин узлов реестра стадий не даёт: его инструменты вне графа."""
    return {}


def _bash_nodes(cfg: None) -> Mapping[str, StageNode]:
    return BashStage.stages()


def _doc_nodes(cfg: DocToolsConfig) -> Mapping[str, StageNode]:
    return DocEngine.stages(cfg)


def _chart_nodes(cfg: None) -> Mapping[str, StageNode]:
    return ChartCaller.stages()


def _web_nodes(cfg: WebGrepConfig) -> Mapping[str, StageNode]:
    return WebStages(cfg).stages()


def _pg_nodes(cfg: PgExecutorConfig) -> Mapping[str, StageNode]:
    return PgStages.of(cfg)


def _ch_nodes(cfg: ChExecutorConfig) -> Mapping[str, StageNode]:
    return ChStages.of(cfg)


def _kb_nodes(cfg: PostgresKnowledgeBaseConfig) -> Mapping[str, StageNode]:
    return KbStages.of(cfg)


def _confluence_nodes(cfg: ConfluenceToolsConfig) -> Mapping[str, StageNode]:
    """Чтение Confluence плюс узлы разбора разметки: профиль у них общий."""
    nodes: dict[str, StageNode] = {}
    nodes.update(ConfluenceStages.of(cfg))
    nodes.update(HtmlStages.of())

    return nodes


def _ingest_nodes(cfg: ConfluenceIngestConfig) -> Mapping[str, StageNode]:
    return ConfluenceIngestStages.of(cfg)


def _build_sandbox_tools(build: PluginBuild) -> list[BaseTool]:
    profile = build.profile_of(BashStage.NAME)

    return [build_bash_tool(build.launchers, profile.max_output_bytes)]


def _build_doc_tools(build: PluginBuild) -> list[BaseTool]:
    return build_doc_tools(build.cfg, build.launchers)


def _build_ingest_tools(build: PluginBuild) -> list[BaseTool]:
    return build_confluence_ingest_tools(build.cfg, build.launchers)


def _build_confluence_tools(build: PluginBuild) -> list[BaseTool]:
    return build_confluence_tools(build.cfg, build.launchers)


def _build_web_tools(build: PluginBuild) -> list[BaseTool]:
    return build_web_tools(build.cfg, build.launchers)


def _build_chart_tools(build: PluginBuild) -> list[BaseTool]:
    return build_chart_tools(build.launchers)


def _build_pg_tools(build: PluginBuild) -> list[BaseTool]:
    return build_pg_tools(build.cfg, build.launchers)


def _build_ch_tools(build: PluginBuild) -> list[BaseTool]:
    return build_ch_tools(build.cfg, build.launchers)


def _build_kb_tools(build: PluginBuild) -> list[BaseTool]:
    return build_kb_tools(build.cfg, build.launchers)


def _build_send_file_tools(build: PluginBuild) -> list[BaseTool]:
    from boba.chainlit.agent.tools.send_file import (  # noqa: PLC0415
        build_send_file_tool,
    )

    return [build_send_file_tool()]


def _build_diagram_tools(build: PluginBuild) -> list[BaseTool]:
    from boba.chainlit.agent.tools.diagram import (  # noqa: PLC0415
        build_diagram_tools,
    )

    return build_diagram_tools(build.cfg)


def _build_canvas_tools(build: PluginBuild) -> list[BaseTool]:
    from boba.chainlit.agent.tools.canvas import (  # noqa: PLC0415
        build_canvas_tools,
    )

    return build_canvas_tools(build.cfg)


def _build_stream_logs_tools(build: PluginBuild) -> list[BaseTool]:
    from boba.chainlit.agent.tools.stream_logs import (  # noqa: PLC0415
        build_stream_logs_tools,
    )

    return build_stream_logs_tools(build.cfg)


def _sandbox_path_vars() -> dict[str, str]:
    """Значения {user_id}/{thread_id} для путей профиля на момент вызова."""
    values = {"user_id": current_user_id(), "thread_id": current_thread_id()}
    return {name: str(value) for name, value in values.items() if value}


def _no_launchers(tool: str) -> ToolLauncher:
    """Плагин объявлен без песочницы: запускать в ней нечего."""
    msg = f"tool {tool!r} is registered without a sandbox profile"
    raise RuntimeError(msg)


class SharedLauncher:
    """Фабрика порта запуска: исполнитель один, профиль стадии живёт в её узле."""

    def __init__(self, caller: SandboxCaller) -> None:
        self._caller = caller

    def __call__(self, tool: str, /) -> ToolLauncher:
        return self._caller


class ToolPlugins:
    """Состав плагинов приложения: секция [tool.<name>] -> описание плагина."""

    ALL: ClassVar[Mapping[str, ToolPlugin]] = {
        "bash": ToolPlugin(
            section="bash",
            build=_build_sandbox_tools,
            nodes=_bash_nodes,
        ),
        "doc": ToolPlugin(
            section="doc",
            config_model=DocToolsConfig,
            build=_build_doc_tools,
            nodes=_doc_nodes,
        ),
        "chart": ToolPlugin(
            section="chart",
            build=_build_chart_tools,
            nodes=_chart_nodes,
        ),
        "send_file": ToolPlugin(
            section="send_file",
            build=_build_send_file_tools,
            nodes=_no_nodes,
            sandboxed=False,
        ),
        "diagram": ToolPlugin(
            section="diagram",
            config_model=DiagramToolConfig,
            build=_build_diagram_tools,
            nodes=_no_nodes,
            sandboxed=False,
        ),
        "canvas": ToolPlugin(
            section="canvas",
            config_model=CanvasToolConfig,
            build=_build_canvas_tools,
            nodes=_no_nodes,
            sandboxed=False,
        ),
        "stream_logs": ToolPlugin(
            section="stream_logs",
            build=_build_stream_logs_tools,
            nodes=_no_nodes,
            sandboxed=False,
        ),
        "pg": ToolPlugin(
            section="pg",
            config_model=PgExecutorConfig,
            build=_build_pg_tools,
            nodes=_pg_nodes,
        ),
        "ch": ToolPlugin(
            section="ch",
            config_model=ChExecutorConfig,
            build=_build_ch_tools,
            nodes=_ch_nodes,
        ),
        "kb": ToolPlugin(
            section="kb",
            config_model=PostgresKnowledgeBaseConfig,
            build=_build_kb_tools,
            nodes=_kb_nodes,
        ),
        "confluence": ToolPlugin(
            section="confluence",
            config_model=ConfluenceToolsConfig,
            build=_build_confluence_tools,
            nodes=_confluence_nodes,
        ),
        "ingest": ToolPlugin(
            section="ingest",
            config_model=ConfluenceIngestConfig,
            build=_build_ingest_tools,
            nodes=_ingest_nodes,
        ),
        "web": ToolPlugin(
            section="web",
            config_model=WebGrepConfig,
            build=_build_web_tools,
            nodes=_web_nodes,
        ),
    }

    @classmethod
    def of(cls, section: str) -> ToolPlugin:
        """Плагин секции; неизвестное имя — нарушение инварианта сборки."""
        plugin = cls.ALL.get(section)
        if plugin is None:
            msg = f"unknown tool plugin section: {section}"
            raise RuntimeError(msg)

        return plugin


@dataclass(frozen=True)
class ToolSection:
    """Разобранная секция [tool.<name>]: мета, конфиг и узлы стадий с профилем."""

    name: str
    plugin: ToolPlugin
    meta: PluginMeta
    cfg: ConfigT
    nodes: Mapping[str, StageDef]
    profile: SandboxProfile | None
    """Профиль песочницы секции; None — плагин работает вне песочницы."""

    def sandbox_missing(self) -> bool:
        """Секции нужна песочница, а bwrap в доверенных каталогах нет."""
        if self.profile is None:
            return False

        return not has_bwrap(self.profile)

    def launchers(self, shared: LauncherFactory) -> LauncherFactory:
        """Плагину без песочницы порт запуска не положен."""
        if not self.plugin.sandboxed:
            return _no_launchers

        return shared

    def node_roles(self) -> dict[str, list[str]]:
        """Права узлов секции: объявленные в конфиге рядом с фасадами.

        Узел без записи в tools = {…} прав не получает — deny by default.
        """
        roles: dict[str, list[str]] = {}
        for node in self.nodes:
            if node not in self.meta.tools:
                continue
            roles[node] = self.meta.roles_of(node)

        return roles


class ToolSections:
    """Разбор включённых секций [tool.*] и сборка из них реестра стадий."""

    @classmethod
    def enabled(
        cls,
        raw_config: DictConfig,
        plugins: Mapping[str, ToolPlugin],
    ) -> list[ToolSection]:
        """Секции с enable = true: конфиг плагина и его узлы с профилем секции."""
        sections: list[ToolSection] = []
        for name, plugin in plugins.items():
            meta = bind(raw_config, f"tool.{name}", PluginMeta)
            if not meta.enable:
                continue

            cfg: ConfigT = None
            if plugin.config_model is not None:
                cfg = bind(raw_config, f"tool.{name}", plugin.config_model)

            profile = cls._profile(raw_config, name, plugin)

            sections.append(
                ToolSection(
                    name=name,
                    plugin=plugin,
                    meta=meta,
                    cfg=cfg,
                    nodes=cls._nodes(plugin, cfg, profile),
                    profile=profile,
                )
            )

        return sections

    @staticmethod
    def registry(sections: Iterable[ToolSection]) -> StageRegistry:
        """Боевой реестр стадий: узлы всех включённых секций в одном пространстве."""
        defs: dict[str, StageDef] = {}
        for section in sections:
            defs.update(section.nodes)

        return StageRegistry(defs)

    @staticmethod
    def _profile(
        raw_config: DictConfig,
        name: str,
        plugin: ToolPlugin,
    ) -> SandboxProfile | None:
        """Профиль секции; None — плагин без песочницы, профиля у него нет."""
        if not plugin.sandboxed:
            return None

        sandbox = bind(raw_config, f"tool.{name}.sandbox", SandboxToolConfig)

        return sandbox.effective()

    @staticmethod
    def _nodes(
        plugin: ToolPlugin,
        cfg: ConfigT,
        profile: SandboxProfile | None,
    ) -> dict[str, StageDef]:
        if profile is None:
            return {}

        defs: dict[str, StageDef] = {}
        for node_name, node in plugin.nodes(cfg).items():
            defs[node_name] = StageDef.of(node, profile)

        return defs


class NodeAccess:
    """Право на узел workflow: карта прав подставляется после сборки реестра.

    Предикат уезжает в исполнитель до того, как ToolAccess построен;
    вызовы случаются только при исполнении графа — к этому моменту bind
    обязан состояться, иначе проверка падает явной ошибкой.
    """

    def __init__(self) -> None:
        self._access: ToolAccess | None = None

    def bind(self, access: ToolAccess) -> None:
        self._access = access

    def __call__(self, tool_name: str, /) -> bool:
        if self._access is None:
            msg = "workflow node access is checked before the tool registry is built"
            raise RuntimeError(msg)

        return self._access.allowed(tool_name, current_user_roles())


@dataclass(frozen=True)
class ToolRegistry:
    """Собранные инструменты и права доступа к ним"""

    tools: list[BaseTool]
    access: ToolAccess

    def for_roles(self, user_roles: Iterable[str]) -> list[BaseTool]:
        roles = frozenset(user_roles)
        allowed = [t for t in self.tools if self.access.allowed(t.name, roles)]
        logger.info(
            "tools available: %d of %d (roles: %s)",
            len(allowed),
            len(self.tools),
            sorted(roles) or "нет",
        )
        return allowed


def _stage_tools(section: ToolSection, built: list[BaseTool]) -> list[str]:
    """Инструменты секции с узлами: их вызовы идут стадиями и пишут журнал."""
    if not section.nodes:
        return []

    names: list[str] = []
    for tool in built:
        names.append(tool.name)

    return names


def _bind_panel(registry: StageRegistry, stage_tools: Iterable[str]) -> None:
    """Контракты узлов и инструменты со стадиями — панели.

    По ним панель выбирает канал журнала и ставит кнопку вывода на шаг.
    """
    contracts: dict[str, StageContract] = {}
    for name in registry.names():
        contracts[name] = registry.def_of(name).contract

    StageTools.configure(stage_tools, contracts)


def _build_section(section: ToolSection, shared: LauncherFactory) -> list[BaseTool]:
    """Инструменты секции, оставленные allowlist'ом tools = {…}."""
    if section.sandbox_missing():
        logger.warning(
            "[tool.%s] is enabled, but bubblewrap (bwrap) is not in the "
            "trusted binary directories — its tools were not registered",
            section.name,
        )
        return []

    build = PluginBuild(
        cfg=section.cfg,
        launchers=section.launchers(shared),
        nodes=section.nodes,
    )

    built: list[BaseTool] = []
    for tool in section.plugin.build(build):
        if tool.name not in section.meta.tools:
            continue
        built.append(tool)

    return built


def _load_workflow_tools(
    raw_config: DictConfig,
    tools: list[BaseTool],
    roles_by_tool: dict[str, list[str]],
    launcher: ToolLauncher,
    registry: StageRegistry,
) -> list[str]:
    """Инструмент workflow: граф стадий поверх боевого реестра узлов.

    Возвращает имена зарегистрированных инструментов: их вызовы идут стадиями.
    """
    from boba.chainlit.agent.tools.workflow import (  # noqa: PLC0415
        build_workflow_tool,
    )

    meta = bind(raw_config, "tool.workflow", PluginMeta)
    if not meta.enable:
        return []

    if not registry.names():
        logger.warning(
            "[tool.workflow] is enabled, but the stage registry is empty — "
            "workflow was not registered",
        )
        return []

    built = build_workflow_tool(launcher)
    if built.name not in meta.tools:
        return []

    roles_by_tool[built.name] = meta.roles_of(built.name)
    tools.append(built)

    return [built.name]


def load_tools(raw_config: DictConfig) -> ToolRegistry:
    sections = ToolSections.enabled(raw_config, ToolPlugins.ALL)

    registry = ToolSections.registry(sections)
    logger.info("stage registry: %s", ", ".join(sorted(registry.names())) or "empty")

    node_access = NodeAccess()
    caller = SandboxCaller(
        registry, node_access, _sandbox_path_vars, StreamJournalHub.get()
    )
    shared = SharedLauncher(caller)

    tools: list[BaseTool] = []
    stage_tools: list[str] = []
    roles_by_tool: dict[str, list[str]] = {}
    for section in sections:
        built = _build_section(section, shared)

        for tool in built:
            roles_by_tool[tool.name] = section.meta.roles_of(tool.name)

        roles_by_tool.update(section.node_roles())

        tools.extend(built)

        stage_tools.extend(_stage_tools(section, built))

    stage_tools.extend(
        _load_workflow_tools(raw_config, tools, roles_by_tool, caller, registry)
    )

    _bind_panel(registry, stage_tools)

    access = ToolAccess(roles_by_tool)
    node_access.bind(access)
    ToolRunLogger.guard_all(tools)
    CancellableTools.guard_all(tools)
    ToolAccessGuard.guard_all(tools, access, current_user_roles)
    ToolErrorGuard.guard_all(tools)
    return ToolRegistry(tools=tools, access=access)
