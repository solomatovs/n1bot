"""Реестр tool-плагинов: секция [tool.<name>] -> langchain-инструменты."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict

from boba.chainlit.agent.toolrun.access import ToolAccess, ToolAccessGuard
from boba.chainlit.agent.toolrun.call_id import ToolCallIdField
from boba.chainlit.agent.toolrun.cancellation import CancellableTools
from boba.chainlit.agent.toolrun.errors import ToolErrorGuard
from boba.chainlit.agent.toolrun.injected import InjectedConfig, ToolConfigError
from boba.chainlit.agent.toolrun.intent import ToolIntentField
from boba.chainlit.agent.toolrun.run_log import CallStream, ToolRunLogger
from boba.chainlit.agent.toolrun.wrapping import ToolAsyncBody
from boba.chainlit.agent.tools.send_file import build_send_file_tool
from boba.chainlit.canvas.diagram import (
    DiagramToolConfig,
    build_diagram_tools,
)
from boba.chainlit.canvas.panel import ToolStreams
from boba.chainlit.canvas.stream_logs import build_stream_logs_tools
from boba.chainlit.canvas.tools import CanvasToolConfig, build_canvas_tools
from boba.chainlit.connections.store import ConnectionKind, ConnectionsConfig
from boba.chainlit.connections.whitelist import ConnectionKeying
from boba.chainlit.domain.keys import WorkspaceMount
from boba.chainlit.domain.session import (
    current_chat_profile,
    current_thread_id,
    current_user,
    current_user_id,
    current_user_label,
    current_user_roles,
)
from boba.chainlit.infra.config import ProfilesSection, RolesSection
from boba.chainlit.infra.tickets import ServiceTickets
from boba.chainlit.infra.user_connections import (
    RegistryRef,
    StoreRef,
    UserConnections,
    UserConnectionsSpec,
)
from boba.sandbox import (
    SandboxProfile,
    SandboxToolConfig,
    has_bwrap,
)
from boba.sandbox.zygote import ZygotePolicy, ZygoteRegistry, ZygoteToolCaller
from boba.settings import bind
from boba.tool.ch.tools import TOOLS as CH_TOOLS
from boba.tool.chart.tools import TOOLS as CHART_TOOLS
from boba.tool.doc.tools import TOOLS as DOC_TOOLS
from boba.tool.kb.confluence.ingest_tools import TOOLS as INGEST_TOOLS
from boba.tool.kb.confluence.tools import TOOLS as CONFLUENCE_TOOLS
from boba.tool.kb.tools import TOOLS as KB_TOOLS
from boba.tool.pg.tools import TOOLS as PG_TOOLS
from boba.tool.shell.tools import BashToolConfig, build_bash_tool
from boba.tool.web.tools import TOOLS as WEB_TOOLS
from boba.toolkit.entry import ToolAddress, ToolArgv, ToolLike, ToolMain
from boba.toolkit.facade import PayloadTool, WarmupHooks
from boba.toolkit.launcher import LauncherFactory, ToolLauncher
from boba.toolkit.types import StringList
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolkit.zygote import WarmupCall

__all__ = [
    "PluginMeta",
    "ToolPlugin",
    "ToolRegistry",
    "as_structured_tool",
    "load_tools",
    "stream_source",
    "warmup_configs",
]

logger = logging.getLogger(__name__)

ConfigT = Any


@dataclass(frozen=True)
class ToolPlugin:
    """Один tool-плагин: как собрать инструменты из секции [tool.<name>]."""

    section: str
    build: Callable[[Any, LauncherFactory], list[BaseTool]] | None = None
    config_model: type[BaseModel] | None = None
    sandboxed: bool = True
    """False — инструменты плагина ничего не запускают, секции sandbox у него нет."""
    module_tools: tuple[BaseTool, ...] = ()
    """Функции уровня модуля новой модели: обёртка запуска ставится на них."""
    modules: tuple[str, ...] = ()
    """Модули тел module_tools: их прогревает зигота секции."""
    connections: UserConnectionsSpec | None = None
    """Whitelist соединений секции собирается из таблицы на вызов; None — секция
    соединений пользователя не держит."""


class SandboxRequire(BaseModel):
    """Кусок секции [sandbox], который читает загрузчик инструментов."""

    model_config = ConfigDict(extra="ignore")

    zygote: ZygotePolicy
    """Политика супервизора зигот; каждая sandboxed-секция обслуживается зиготой."""


class PluginMeta(BaseModel):
    """Meta-конфиг плагина: что framework читает из [tool.<name>]."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    tools: StringList = []


def _build_sandbox_tools(
    cfg: BashToolConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return [as_structured_tool(build_bash_tool(cfg, launchers))]


def _build_send_file_tools(
    cfg: None,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return [build_send_file_tool()]


def _build_diagram_tools(
    cfg: DiagramToolConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return build_diagram_tools(cfg)


def _build_canvas_tools(
    cfg: CanvasToolConfig,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return build_canvas_tools(cfg)


def _build_stream_logs_tools(
    cfg: None,
    launchers: LauncherFactory,
) -> list[BaseTool]:
    return build_stream_logs_tools(cfg)


def _modules_of(tools: Sequence[ToolLike]) -> tuple[str, ...]:
    """Уникальные модули тел, в порядке объявления."""
    modules: list[str] = []
    for tool in tools:
        module = ToolAddress.of(tool).module
        if module not in modules:
            modules.append(module)

    return tuple(modules)


def _module_toolset(tools: Sequence[ToolLike]) -> tuple[BaseTool, ...]:
    """TOOLS модуля инструментов -> langchain-инструменты для реестра.

    Статически TOOLS — ToolLike (toolkit не знает langchain): модули
    объявляются фасадом PayloadTool и langchain не импортируют. Мост в
    BaseTool делается здесь, на стороне приложения.
    """
    checked: list[BaseTool] = []
    for tool in tools:
        checked.append(as_structured_tool(tool))

    return tuple(checked)


def as_structured_tool(tool: ToolLike) -> BaseTool:
    """PayloadTool фасада -> StructuredTool; langchain-инструмент — как есть.

    Injected-параметры остаются в args_schema: их снимает InjectedConfig
    после постановки обёртки запуска, LLM усечённую схему и увидит.
    """
    if isinstance(tool, BaseTool):
        return tool

    if not isinstance(tool, PayloadTool):
        msg = f"module tool {tool!r} is neither PayloadTool nor BaseTool"
        raise TypeError(msg)

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        func=tool.func,
        coroutine=tool.coroutine,
        response_format=PayloadTool.RESPONSE_FORMAT,
    )


def stream_source(tool: str, call_id: str) -> CallStream | None:
    """Журнал живого вывода вызова; тред и пользователь — из сессии."""
    if not ToolStreams.streamable(tool):
        return None

    thread_id = current_thread_id()
    user_id = current_user_id()
    if thread_id is None or user_id is None:
        return None

    return ToolStreams.begin(str(user_id), thread_id, call_id, tool)


def _sandbox_path_vars() -> dict[str, str]:
    """Значения {user_id}/{thread_id} для путей профиля на момент вызова.

    id есть только у пользователя, сохранённого слоем данных: вход, чей
    create_user не прошёл, живёт в сессии как cl.User, и образ workspace
    такому вызову не собрать.
    """
    values = {"user_id": current_user_id(), "thread_id": current_thread_id()}

    ready: dict[str, str] = {}
    for name, value in values.items():
        if not value:
            continue

        ready[name] = str(value)

    if current_user() is not None and "user_id" not in ready:
        logger.error(
            "sandbox: session user %r has no id: the sign-in was not persisted",
            current_user_label(),
        )

    return ready


def _enabled_tools(
    plugin: ToolPlugin,
    cfg: ConfigT,
    launchers: LauncherFactory,
    meta: PluginMeta,
) -> list[BaseTool]:
    """Инструменты фабрики плагина, перечисленные в [tool.<name>] tools."""
    if plugin.build is None:
        return []

    built: list[BaseTool] = []

    for tool in plugin.build(cfg, launchers):
        if tool.name not in meta.tools:
            continue

        built.append(tool)

    return built


def _sandbox_section(raw_config: DictConfig, name: str) -> SandboxProfile | None:
    """Профиль секции [tool.<name>.sandbox]; None — секции нет, запуск локальный.

    Секция есть, а bwrap недоступен — отказ старта: молчаливая деградация
    инструмента до процесса приложения запрещена.
    """
    node = OmegaConf.select(raw_config, f"tool.{name}.sandbox")
    if node is None:
        return None

    sandbox = bind(raw_config, f"tool.{name}.sandbox", SandboxToolConfig)
    profile = sandbox.profile

    if profile.mounts.workspace is not None:
        WorkspaceMount.configure(profile.mounts.workspace.mount.target)

    if not has_bwrap(profile):
        msg = (
            f"[tool.{name}.sandbox] is configured, but bubblewrap (bwrap) "
            "is not in the trusted binary directories"
        )
        raise RuntimeError(msg)

    return profile


def _config_resolver(raw_config: DictConfig) -> Callable[[str, Any], object]:
    """Значения injected-параметров: модель собирается из своей секции."""

    def resolve(param: str, annotation: Any) -> object:
        section = getattr(annotation, "SECTION", None)
        if not isinstance(section, str):
            msg = f"injected parameter {param!r} has no SECTION on its model"
            raise ToolConfigError(msg)

        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            msg = f"injected parameter {param!r} is not a pydantic model"
            raise ToolConfigError(msg)

        return bind(raw_config, section, annotation)

    return resolve


def _no_launchers(tool: str) -> ToolLauncher:
    """Плагин объявлен без песочницы: запускать в ней нечего."""
    msg = f"tool {tool!r} is registered without a sandbox profile"
    raise RuntimeError(msg)


_PLUGINS: dict[str, ToolPlugin] = {
    "bash": ToolPlugin(
        section="bash",
        config_model=BashToolConfig,
        build=_build_sandbox_tools,
    ),
    "doc": ToolPlugin(
        section="doc",
        module_tools=_module_toolset(DOC_TOOLS),
        modules=_modules_of(DOC_TOOLS),
    ),
    "chart": ToolPlugin(
        section="chart",
        module_tools=_module_toolset(CHART_TOOLS),
        modules=_modules_of(CHART_TOOLS),
    ),
    "send_file": ToolPlugin(
        section="send_file",
        build=_build_send_file_tools,
        sandboxed=False,
    ),
    "diagram": ToolPlugin(
        section="diagram",
        config_model=DiagramToolConfig,
        build=_build_diagram_tools,
        sandboxed=False,
    ),
    "canvas": ToolPlugin(
        section="canvas",
        config_model=CanvasToolConfig,
        build=_build_canvas_tools,
        sandboxed=False,
    ),
    "stream_logs": ToolPlugin(
        section="stream_logs",
        build=_build_stream_logs_tools,
        sandboxed=False,
    ),
    "pg": ToolPlugin(
        section="pg",
        module_tools=_module_toolset(PG_TOOLS),
        modules=_modules_of(PG_TOOLS),
        connections=UserConnectionsSpec(ConnectionKind.POSTGRES, ConnectionKeying.NAME),
    ),
    "ch": ToolPlugin(
        section="ch",
        module_tools=_module_toolset(CH_TOOLS),
        modules=_modules_of(CH_TOOLS),
        connections=UserConnectionsSpec(
            ConnectionKind.CLICKHOUSE, ConnectionKeying.NAME
        ),
    ),
    "kb": ToolPlugin(
        section="kb",
        module_tools=_module_toolset(KB_TOOLS),
        modules=_modules_of(KB_TOOLS),
    ),
    "confluence": ToolPlugin(
        section="confluence",
        module_tools=_module_toolset(CONFLUENCE_TOOLS),
        modules=_modules_of(CONFLUENCE_TOOLS),
    ),
    "ingest": ToolPlugin(
        section="ingest",
        module_tools=_module_toolset(INGEST_TOOLS),
        modules=_modules_of(INGEST_TOOLS),
    ),
    "web": ToolPlugin(
        section="web",
        module_tools=_module_toolset(WEB_TOOLS),
        modules=_modules_of(WEB_TOOLS),
        connections=UserConnectionsSpec(ConnectionKind.WEB, ConnectionKeying.NAME),
    ),
}


@dataclass(frozen=True)
class ToolRegistry:
    """Собранные инструменты и права доступа к ним"""

    tools: list[BaseTool]
    access: ToolAccess

    def for_session(
        self,
        user_roles: Iterable[str],
        profile: str | None,
    ) -> list[BaseTool]:
        roles = frozenset(user_roles)

        allowed: list[BaseTool] = []
        for tool in self.tools:
            if self.access.allowed(tool.name, roles, profile):
                allowed.append(tool)

        logger.info(
            "tools available: %d of %d (roles: %s, chat profile: %s)",
            len(allowed),
            len(self.tools),
            sorted(roles) or "none",
            profile or "none",
        )
        return allowed


def _module_tools(  # noqa: PLR0913 — состав загрузки секции задаётся целиком
    plugin: ToolPlugin,
    meta: PluginMeta,
    profile: SandboxProfile,
    raw_config: DictConfig,
    zygote_policy: ZygotePolicy,
    store_ref: StoreRef,
    registry_ref: RegistryRef,
) -> list[BaseTool]:
    """Функции модуля новой модели: обёртка запуска + partial конфига.

    Обвязки ставятся на копии: load_tools зовётся не один раз (bootstrap,
    DI-провайдер), а модульные TOOLS — синглтоны процесса, и повторная
    обёртка поверх уже обёрнутого ломала бы адрес тела и схему.
    """
    functions: list[BaseTool] = []
    for tool in plugin.module_tools:
        if tool.name not in meta.tools:
            continue

        functions.append(tool.model_copy())

    if not functions:
        return []

    launcher = _section_launcher(plugin, profile, zygote_policy, raw_config)

    ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

    resolve = _config_resolver(raw_config)
    if plugin.connections is not None:
        UserConnections.bind_all(
            functions, store_ref, registry_ref, plugin.connections, resolve
        )

    ServiceTickets.bind_all(functions, resolve)
    InjectedConfig.bind_all(functions, resolve)

    return functions


def _section_launcher(
    plugin: ToolPlugin,
    profile: SandboxProfile,
    zygote_policy: ZygotePolicy,
    raw_config: DictConfig,
) -> ToolLauncher:
    """Исполнитель секции: зигота секции, одна на все её инструменты."""
    supervisor = ZygoteRegistry.obtain(
        plugin.section,
        profile,
        plugin.modules,
        zygote_policy,
        warmup_calls=warmup_configs(plugin.section, plugin.modules, raw_config),
    )

    return ZygoteToolCaller(plugin.section, supervisor, profile, _sandbox_path_vars)


def warmup_configs(
    section: str, modules: Sequence[str], raw_config: DictConfig
) -> tuple[WarmupCall, ...]:
    """Прогревы модулей секции: конфиг каждому хуку из секции его инструмента.

    Хуки объявляет автор инструмента через @warmup, хост их не угадывает —
    берёт из реестра фасада. Модель конфига — аннотация параметра хука,
    значения приезжают из [tool.<section>].
    """
    calls: list[WarmupCall] = []

    for name in modules:
        for hook in WarmupHooks.of(name):
            model = bind(raw_config, f"tool.{section}", hook.config_model)
            calls.append(
                WarmupCall(
                    module=name,
                    hook=hook.name,
                    config=ToolArgv.reveal(hook.config_model, model),
                )
            )

    return tuple(calls)


def _plugin_sandbox(
    name: str, plugin: ToolPlugin, raw_config: DictConfig
) -> SandboxProfile | None:
    """Профиль песочницы плагина; None — только у плагинов процесса приложения."""
    if not plugin.sandboxed:
        return None

    profile = _sandbox_section(raw_config, name)

    if profile is None:
        msg = f"[tool.{name}.sandbox] is missing: the plugin runs in a sandbox"
        raise RuntimeError(msg)

    return profile


def _plugin_tools(  # noqa: PLR0913 — состав загрузки секции задаётся целиком
    name: str,
    plugin: ToolPlugin,
    meta: PluginMeta,
    profile: SandboxProfile | None,
    raw_config: DictConfig,
    zygote_policy: ZygotePolicy,
    store_ref: StoreRef,
    registry_ref: RegistryRef,
) -> list[BaseTool]:
    """Инструменты плагина: фабричные старого пути плюс функции модуля."""
    cfg: ConfigT = None
    if plugin.config_model is not None:
        cfg = bind(raw_config, f"tool.{name}", plugin.config_model)

    if profile is None:
        return _enabled_tools(plugin, cfg, _no_launchers, meta)

    launchers = _section_launchers(plugin, profile, zygote_policy, raw_config)
    built = _enabled_tools(plugin, cfg, launchers, meta)

    built.extend(
        _module_tools(
            plugin, meta, profile, raw_config, zygote_policy, store_ref, registry_ref
        )
    )
    return built


def _section_launchers(
    plugin: ToolPlugin,
    profile: SandboxProfile,
    zygote_policy: ZygotePolicy,
    raw_config: DictConfig,
) -> LauncherFactory:
    """Фабрика исполнителей фабричных инструментов: одна зигота секции на всех."""
    launcher = _section_launcher(plugin, profile, zygote_policy, raw_config)

    def factory(tool: str) -> ToolLauncher:
        return launcher

    return factory


def _access_of(raw_config: DictConfig, tools: Sequence[BaseTool]) -> ToolAccess:
    """Права из [roles.*]/[profiles.*]; опечатка в имени инструмента — отказ старта."""
    roles = bind(raw_config, "roles", RolesSection).root
    profiles = bind(raw_config, "profiles", ProfilesSection).root

    known = frozenset(tool.name for tool in tools)

    for role_name, grant in roles.items():
        missing = grant.unknown(known)
        if missing:
            msg = f"[roles.{role_name}]: unknown tools {missing}"
            raise RuntimeError(msg)

    for profile_name, profile in profiles.items():
        missing = profile.unknown(known)
        if missing:
            msg = f"[profiles.{profile_name}]: unknown tools {missing}"
            raise RuntimeError(msg)

    return ToolAccess(known, roles, profiles)


def _require_connections(raw_config: DictConfig, name: str) -> None:
    """Секция с соединениями пользователя работает только при [connections]."""
    cfg = bind(raw_config, "connections", ConnectionsConfig)
    if cfg.enable:
        return

    msg = (
        f"[tool.{name}] takes its connections from the connections table: "
        "set [connections] enable = true"
    )
    raise RuntimeError(msg)


def load_tools(
    raw_config: DictConfig, store_ref: StoreRef, registry_ref: RegistryRef
) -> ToolRegistry:
    zygote_policy = bind(raw_config, "sandbox", SandboxRequire).zygote

    tools: list[BaseTool] = []
    for name, plugin in _PLUGINS.items():
        meta = bind(raw_config, f"tool.{name}", PluginMeta)
        if not meta.enable:
            continue

        if plugin.connections is not None:
            _require_connections(raw_config, name)

        profile = _plugin_sandbox(name, plugin, raw_config)
        built = _plugin_tools(
            name,
            plugin,
            meta,
            profile,
            raw_config,
            zygote_policy,
            store_ref,
            registry_ref,
        )
        tools.extend(built)

        # живой вывод есть только у процессов песочницы: кнопка потока
        # рисуется на шагах этих инструментов
        if profile is not None:
            streamable: list[str] = []
            for tool in built:
                streamable.append(tool.name)
            ToolStreams.mark_streamable(streamable)

    access = _access_of(raw_config, tools)
    ToolCallIdField.attach_all(tools)
    ToolIntentField.attach_all(tools)
    ToolRunLogger.guard_all(tools, stream_source)
    CancellableTools.guard_all(tools)
    ToolAccessGuard.guard_all(tools, access, current_user_roles, current_chat_profile)
    ToolErrorGuard.guard_all(tools)
    ToolAsyncBody.ensure_all(tools)
    return ToolRegistry(tools=tools, access=access)
