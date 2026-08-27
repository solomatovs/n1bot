"""Загрузчик tool-плагинов: секция [tool.<name>] -> langchain-инструменты с обвязками.

Ошибки:
RuntimeError — конфиг противоречит плагину: нет [tool.<name>.sandbox]
    у sandboxed-плагина, bwrap недоступен, секция с соединениями
    пользователя без [connections].
ToolConfigError — injected-параметр инструмента не привязан к секции конфига.
TypeError — TOOLS модуля содержит не PayloadTool и не BaseTool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict

from boba.access import ToolAccess
from boba.canvas.keys import WorkspaceMount
from boba.chat.profiles import ProfilesSection, RolesSection
from boba.connection_broker.store import ConnectionsConfig
from boba.connection_broker.tickets import ServiceTickets
from boba.connection_broker.user_connections import (
    RefreshSignal,
    StoreRef,
    TicketsRef,
    UserConnections,
)
from boba.connections.marks import UserConnectionsSpec
from boba.connections.profile import ConnectionKind
from boba.connections.whitelist import ConnectionKeying
from boba.identity.context import CallContext
from boba.runtime.refs import RuntimeRefs
from boba.sandbox import (
    BindSpec,
    SandboxProfile,
    SandboxToolConfig,
    has_bwrap,
)
from boba.sandbox.guest import WarmupCall
from boba.sandbox.wrap import ToolProcessWrap
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
from boba.toolkit.entry import ToolAddress, ToolArgv, ToolEntryError, ToolLike, ToolMain
from boba.toolkit.facade import PayloadTool, WarmupHooks
from boba.toolkit.launcher import LauncherFactory, ToolLauncher
from boba.toolkit.types import StringList
from boba.toolrun.access import ToolAccessGuard
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.cancellation import CancellableTools
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.injected import InjectedConfig, ToolConfigError
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.run_log import CallStream, NoCallScope, ToolRunLogger
from boba.toolrun.streams import ToolStreams
from boba.toolrun.wrapping import ToolAsyncBody
from boba.workflow_engine.tools import WorkflowToolConfig, build_workflow_tools

__all__ = [
    "CallSurface",
    "CoreTools",
    "PluginMeta",
    "PluginTable",
    "SandboxRequire",
    "ToolBridge",
    "ToolLoader",
    "ToolPlugin",
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
    chat_only: bool = False
    """True — инструментам нужна поверхность чата (панель, карточки, вложения):
    вне хода чата они отказывают, в каталог workflow не попадают."""


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


class ToolBridge:
    """Мост TOOLS модулей инструментов в langchain: toolkit langchain не знает."""

    @staticmethod
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

    @classmethod
    def toolset(cls, tools: Sequence[ToolLike]) -> tuple[BaseTool, ...]:
        """TOOLS модуля инструментов -> langchain-инструменты для реестра."""
        checked: list[BaseTool] = []
        for tool in tools:
            checked.append(cls.as_structured_tool(tool))

        return tuple(checked)

    @staticmethod
    def modules_of(tools: Sequence[ToolLike]) -> tuple[str, ...]:
        """Уникальные модули тел, в порядке объявления."""
        modules: list[str] = []
        for tool in tools:
            module = ToolAddress.of(tool).module
            if module not in modules:
                modules.append(module)

        return tuple(modules)


class CallSurface:
    """Значения из контекста вызова для обвязок запуска инструментов."""

    @staticmethod
    def stream_source(tool: str, call_id: str) -> CallStream | None:
        """Журнал живого вывода вызова; область и субъект — из контекста вызова."""
        if not ToolStreams.streamable(tool):
            return None

        context = CallContext.peek()
        if context is None:
            return None

        return ToolStreams.begin(
            context.subject.user_key, context.scope.id, call_id, tool
        )

    @staticmethod
    def tool_call_scope(call_id: str) -> Callable[[], None]:
        """Контекст вызова инструмента моделью на время вызова: инициатор llm.

        Без контекста ставить нечего — снимать тоже.
        """
        context = CallContext.peek()
        if context is None:
            return NoCallScope.leave

        token = CallContext.push(context.as_tool_call(call_id))

        def leave() -> None:
            CallContext.pop(token)

        return leave

    @staticmethod
    def sandbox_path_vars() -> dict[str, str]:
        """Значения {user_id}/{thread_id} для путей профиля на момент вызова.

        Вне контекста вызова значений нет: профиль с такими переменными
        отказывает рендером, называя недостающую.
        """
        context = CallContext.peek()
        if context is None:
            return {}

        return {
            BindSpec.VARS[0]: context.subject.user_key,
            BindSpec.VARS[1]: context.scope.id,
        }


class ToolLoader:
    """Сборка реестра инструментов из включённых секций [tool.<name>].

    Обвязки ставятся на копии модульных TOOLS: загрузка зовётся не один раз
    (bootstrap, DI-провайдер), а TOOLS — синглтоны процесса, и повторная
    обёртка поверх уже обёрнутого ломала бы адрес тела и схему.
    """

    def __init__(
        self,
        raw_config: DictConfig,
        plugins: Mapping[str, ToolPlugin],
        store_ref: StoreRef,
        tickets_ref: TicketsRef,
        refresh: RefreshSignal,
    ) -> None:
        self._raw = raw_config
        self._plugins = plugins
        self._store_ref = store_ref
        self._tickets_ref = tickets_ref
        self._refresh = refresh

    def load(self) -> ToolRegistry:
        zygote_policy = bind(self._raw, "sandbox", SandboxRequire).zygote

        tools: list[BaseTool] = []
        chat_only: set[str] = set()
        for name, plugin in self._plugins.items():
            meta = bind(self._raw, f"tool.{name}", PluginMeta)
            if not meta.enable:
                continue

            if plugin.connections is not None:
                self._require_connections(name)

            profile = self._plugin_sandbox(name, plugin)
            built = self._plugin_tools(name, plugin, meta, profile, zygote_policy)
            tools.extend(built)

            if plugin.chat_only:
                for tool in built:
                    chat_only.add(tool.name)

            # живой вывод есть только у процессов песочницы: кнопка потока
            # рисуется на шагах этих инструментов
            if profile is not None:
                streamable: list[str] = []
                for tool in built:
                    streamable.append(tool.name)
                ToolStreams.mark_streamable(streamable)

        access = self._access_of(tools, chat_only)
        ToolCallIdField.attach_all(tools)
        ToolIntentField.attach_all(tools)
        ToolRunLogger.guard_all(
            tools, CallSurface.stream_source, CallSurface.tool_call_scope
        )
        CancellableTools.guard_all(tools)
        ToolAccessGuard.guard_all(tools, access, CallContext.current_subject)
        ToolErrorGuard.guard_all(tools)
        ToolAsyncBody.ensure_all(tools)
        return ToolRegistry(tools=tools, access=access)

    @staticmethod
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

    def _plugin_tools(
        self,
        name: str,
        plugin: ToolPlugin,
        meta: PluginMeta,
        profile: SandboxProfile | None,
        zygote_policy: ZygotePolicy,
    ) -> list[BaseTool]:
        """Инструменты плагина: фабричные старого пути плюс функции модуля."""
        cfg: ConfigT = None
        if plugin.config_model is not None:
            cfg = bind(self._raw, f"tool.{name}", plugin.config_model)

        if profile is None:
            return self._enabled_tools(plugin, cfg, self._no_launchers, meta)

        launchers = self._section_launchers(plugin, profile, zygote_policy)
        built = self._enabled_tools(plugin, cfg, launchers, meta)

        built.extend(self._module_tools(plugin, meta, profile, zygote_policy))
        return built

    def _module_tools(
        self,
        plugin: ToolPlugin,
        meta: PluginMeta,
        profile: SandboxProfile,
        zygote_policy: ZygotePolicy,
    ) -> list[BaseTool]:
        """Функции модуля новой модели: обёртка запуска + partial конфига."""
        functions: list[BaseTool] = []
        for tool in plugin.module_tools:
            if tool.name not in meta.tools:
                continue

            functions.append(tool.model_copy())

        if not functions:
            return []

        launcher = self._section_launcher(plugin, profile, zygote_policy)

        ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

        resolve = self._config_resolver()
        if plugin.connections is not None:
            UserConnections.bind_all(
                functions,
                self._store_ref,
                self._tickets_ref,
                plugin.connections,
                resolve,
                self._refresh,
            )

        ServiceTickets.bind_all(functions, resolve)
        InjectedConfig.bind_all(functions, resolve)

        return functions

    def _section_launcher(
        self,
        plugin: ToolPlugin,
        profile: SandboxProfile,
        zygote_policy: ZygotePolicy,
    ) -> ToolLauncher:
        """Исполнитель секции: зигота секции, одна на все её инструменты."""
        supervisor = ZygoteRegistry.obtain(
            plugin.section,
            profile,
            plugin.modules,
            zygote_policy,
            warmup_calls=self.warmup_configs(plugin.section, plugin.modules, self._raw),
        )

        return ZygoteToolCaller(
            plugin.section, supervisor, profile, CallSurface.sandbox_path_vars
        )

    def _section_launchers(
        self,
        plugin: ToolPlugin,
        profile: SandboxProfile,
        zygote_policy: ZygotePolicy,
    ) -> LauncherFactory:
        """Фабрика исполнителей фабричных инструментов: одна зигота секции на всех."""
        launcher = self._section_launcher(plugin, profile, zygote_policy)

        def factory(tool: str) -> ToolLauncher:
            return launcher

        return factory

    @staticmethod
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

    def _plugin_sandbox(self, name: str, plugin: ToolPlugin) -> SandboxProfile | None:
        """Профиль песочницы плагина; None — только у плагинов процесса приложения."""
        if not plugin.sandboxed:
            return None

        profile = self._sandbox_section(name)

        if profile is None:
            msg = f"[tool.{name}.sandbox] is missing: the plugin runs in a sandbox"
            raise RuntimeError(msg)

        return profile

    def _sandbox_section(self, name: str) -> SandboxProfile | None:
        """Профиль секции [tool.<name>.sandbox]; None — секции нет, запуск локальный.

        Секция есть, а bwrap недоступен — отказ старта: молчаливая деградация
        инструмента до процесса приложения запрещена.
        """
        node = OmegaConf.select(self._raw, f"tool.{name}.sandbox")
        if node is None:
            return None

        sandbox = bind(self._raw, f"tool.{name}.sandbox", SandboxToolConfig)
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

    def _config_resolver(self) -> Callable[[str, Any], object]:
        """Значения injected-параметров: модель собирается из своей секции."""
        raw = self._raw

        def resolve(param: str, annotation: Any) -> object:
            try:
                section = ToolArgv.section_of(param, annotation)
            except ToolEntryError as exc:
                raise ToolConfigError(str(exc)) from exc

            return bind(raw, section, annotation)

        return resolve

    @staticmethod
    def _no_launchers(tool: str) -> ToolLauncher:
        """Плагин объявлен без песочницы: запускать в ней нечего."""
        msg = f"tool {tool!r} is registered without a sandbox profile"
        raise RuntimeError(msg)

    def _access_of(
        self, tools: Sequence[BaseTool], chat_only: Iterable[str]
    ) -> ToolAccess:
        """Права из [roles.*]/[profiles.*]; опечатка в имени инструмента — отказ."""
        roles = bind(self._raw, "roles", RolesSection).root
        profiles = bind(self._raw, "profiles", ProfilesSection).root
        known = frozenset(tool.name for tool in tools)

        return ToolAccess(known, roles, profiles, chat_only)

    def _require_connections(self, name: str) -> None:
        """Секция с соединениями пользователя работает только при [connections]."""
        cfg = bind(self._raw, "connections", ConnectionsConfig)
        if cfg.enable:
            return

        msg = (
            f"[tool.{name}] takes its connections from the connections table: "
            "set [connections] enable = true"
        )
        raise RuntimeError(msg)


PluginTable = Callable[[RuntimeRefs], Mapping[str, ToolPlugin]]
"""Таблица плагинов процесса: общая часть плюс своё (у чата — chat-only инструменты)."""


class CoreTools:
    """Таблица плагинов, общая для процессов: инструменты модулей, bash и workflow."""

    @classmethod
    def table(cls, refs: RuntimeRefs) -> dict[str, ToolPlugin]:
        return {
            "bash": ToolPlugin(
                section="bash",
                config_model=BashToolConfig,
                build=cls._bash,
            ),
            "doc": cls.module("doc", DOC_TOOLS),
            "chart": cls.module("chart", CHART_TOOLS),
            "workflow": ToolPlugin(
                section="workflow",
                config_model=WorkflowToolConfig,
                build=cls._workflow_builder(refs),
                sandboxed=False,
            ),
            "pg": cls.connected(
                "pg", PG_TOOLS, ConnectionKind.POSTGRES, ConnectionKeying.NAME
            ),
            "ch": cls.connected(
                "ch", CH_TOOLS, ConnectionKind.CLICKHOUSE, ConnectionKeying.NAME
            ),
            "kb": cls.module("kb", KB_TOOLS),
            "confluence": cls.module("confluence", CONFLUENCE_TOOLS),
            "ingest": cls.module("ingest", INGEST_TOOLS),
            "web": cls.connected(
                "web", WEB_TOOLS, ConnectionKind.WEB, ConnectionKeying.NAME
            ),
        }

    @staticmethod
    def module(section: str, tools: Sequence[ToolLike]) -> ToolPlugin:
        return ToolPlugin(
            section=section,
            module_tools=ToolBridge.toolset(tools),
            modules=ToolBridge.modules_of(tools),
        )

    @staticmethod
    def connected(
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
    def _workflow_builder(
        refs: RuntimeRefs,
    ) -> Callable[[WorkflowToolConfig, LauncherFactory], list[BaseTool]]:
        """Сервис workflow берётся из входов приложения на каждый вызов."""

        def build(
            cfg: WorkflowToolConfig, launchers: LauncherFactory
        ) -> list[BaseTool]:
            return build_workflow_tools(cfg, refs.workflow_service)

        return build
