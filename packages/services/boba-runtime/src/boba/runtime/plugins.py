"""Загрузчик tool-плагинов: секция [tool.<name>] -> langchain-инструменты с обвязками.

Плагины обнаруживаются entry points группы boba.tools у установленных пакетов;
конфиг каждого приходит файлом conf/plugins/<name>.toml (слой AppLayers).

Ошибки:
RuntimeError — конфиг противоречит плагину: у установленного плагина нет
    conf/plugins/<name>.toml, entry point отдал не манифест, секции плагинов
    совпали, способ запуска из [tool_launcher] не согласован с секциями
    (см. boba.runtime.launchers), секция с соединениями пользователя без
    [connections].
ToolConfigError — injected-параметр инструмента не привязан к секции конфига.
TypeError — TOOLS модуля содержит не PayloadTool и не BaseTool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict

from boba.access import GrantCheck, ToolAccess
from boba.chat.profiles import ProfilesSection, RolesSection
from boba.config import bind
from boba.connection_broker.catalog import (
    ConnectionCatalogConfig,
    build_connection_tools,
)
from boba.connection_broker.service import UserConnectionsService
from boba.connection_broker.store import ConnectionsConfig
from boba.connection_broker.tickets import ServiceTickets
from boba.connection_broker.user_connections import UserConnections
from boba.identity.context import CallContext
from boba.runtime.launchers import CallSurface, SectionLaunchers, ToolLaunchers
from boba.runtime.refs import RuntimeRefs
from boba.toolkit.entry import ToolAddress, ToolArgv, ToolEntryError, ToolLike, ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.launcher import LauncherFactory, ToolLauncher
from boba.toolkit.manifest import LaunchSpec, ToolPluginManifest
from boba.toolkit.types import StringList
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.access import ToolAccessGuard
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.cancellation import CancellableTools
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.injected import InjectedConfig, ToolConfigError
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.pipeline import PipelineToolConfig, build_pipeline_tools
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.streams import ToolStreams
from boba.toolrun.wrapping import ToolAsyncBody, ToolSchema
from boba.workflow_engine.tools import WorkflowToolConfig, build_workflow_tools

__all__ = [
    "CoreTools",
    "EntryPointPlugins",
    "PluginMeta",
    "PluginTable",
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
    """False — инструменты плагина ничего не запускают; True — тела исполняются
    отдельным процессом способом из [tool_launcher]."""
    module_tools: tuple[BaseTool, ...] = ()
    """Функции уровня модуля новой модели: обёртка запуска ставится на них."""
    modules: tuple[str, ...] = ()
    """Модули тел module_tools: их прогревает зигота секции."""
    chat_only: bool = False
    """True — инструментам нужна поверхность чата (панель, карточки, вложения):
    вне хода чата они отказывают, в каталог workflow не попадают."""
    discovered: bool = False
    """True — плагин пришёл entry point'ом установленного пакета: его секция
    tool.<section> обязана прийти файлом conf/plugins/<section>.toml."""
    package: str = ""
    """Дистрибутив entry point'а: по нему ищется образ корня песочницы
    plugins/<package>/rootfs.ext4; пусто — плагин встроен в приложение."""


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
            msg = (
                f"module tool {tool!r}: expected PayloadTool or langchain "
                f"BaseTool, got {type(tool).__name__}"
            )
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
        refs: RuntimeRefs,
        grant_check: GrantCheck,
    ) -> None:
        self._raw = raw_config
        self._plugins = plugins
        self._store_ref = refs.connection_store
        self._credentials_ref = refs.credentials
        self._types_ref = refs.connection_types
        self._grant_check = grant_check

    def load(self) -> ToolRegistry:
        launchers = ToolLaunchers.of(self._raw)

        tools: list[BaseTool] = []
        chat_only: set[str] = set()
        for name, plugin in self._plugins.items():
            section = OmegaConf.select(self._raw, f"tool.{name}")
            if section is None and plugin.discovered:
                msg = (
                    f"conf/plugins/{name}.toml is missing: the installed "
                    f"plugin {name!r} requires its config"
                )
                raise RuntimeError(msg)

            meta = bind(self._raw, f"tool.{name}", PluginMeta)
            if not meta.enable:
                continue

            built = self._plugin_tools(name, plugin, meta, launchers)
            tools.extend(built)

            if plugin.chat_only:
                for tool in built:
                    chat_only.add(tool.name)

            # живой вывод есть только у отдельных процессов: кнопка потока
            # рисуется на шагах этих инструментов
            if plugin.sandboxed:
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

    def _plugin_tools(
        self,
        name: str,
        plugin: ToolPlugin,
        meta: PluginMeta,
        launchers: SectionLaunchers,
    ) -> list[BaseTool]:
        """Инструменты плагина: фабричные старого пути плюс функции модуля."""
        cfg: ConfigT = None
        if plugin.config_model is not None:
            cfg = bind(self._raw, f"tool.{name}", plugin.config_model)

        if not plugin.sandboxed:
            return self._enabled_tools(plugin, cfg, self._no_launchers, meta)

        spec = LaunchSpec(
            section=plugin.section,
            modules=plugin.modules,
            package=plugin.package,
        )
        launcher = launchers.launcher_of(spec)

        def factory(tool: str) -> ToolLauncher:
            return launcher

        built = self._enabled_tools(plugin, cfg, factory, meta)

        built.extend(self._module_tools(plugin, meta, launcher))
        return built

    def _module_tools(
        self,
        plugin: ToolPlugin,
        meta: PluginMeta,
        launcher: ToolLauncher,
    ) -> list[BaseTool]:
        """Функции модуля новой модели: обёртка запуска + partial конфига."""
        functions: list[BaseTool] = []
        for tool in plugin.module_tools:
            if tool.name not in meta.tools:
                continue

            functions.append(tool.model_copy())

        if not functions:
            return []

        ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

        if self._takes_connections(functions):
            self._require_connections(plugin.section)
            UserConnections.bind_all(
                functions, self._store_ref, self._credentials_ref, self._types_ref
            )

        resolve = self._config_resolver()
        ServiceTickets.bind_all(functions, self._credentials_ref, resolve)
        InjectedConfig.bind_all(functions, resolve)

        return functions

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

    @staticmethod
    def _takes_connections(tools: Sequence[BaseTool]) -> bool:
        """Есть ли у инструментов параметры-соединения: их объявляет подпись."""
        for tool in tools:
            schema = ToolSchema.of(tool)
            if schema is None:
                continue

            if ToolArgv.connection_fields(schema):
                return True

        return False

    def _config_resolver(self) -> Callable[[str, Any], object]:
        """Значения injected-параметров: модель собирается из своей секции."""
        raw = self._raw

        def resolve(param: str, annotation: Any) -> object:
            try:
                section = ToolArgv.section_of(param, annotation)
            except ToolEntryError as exc:
                msg = (
                    f"injected parameter {param!r} annotated {annotation!r}: "
                    f"no config section resolves for it: {exc}"
                )
                raise ToolConfigError(msg) from exc

            return bind(raw, section, annotation)

        return resolve

    @staticmethod
    def _no_launchers(tool: str) -> ToolLauncher:
        """Плагин живёт в процессе приложения: запускать ему нечего."""
        msg = (
            f"tool {tool!r} runs in the app process and has no launcher, "
            "yet a launcher was requested for it"
        )
        raise RuntimeError(msg)

    def _access_of(
        self, tools: Sequence[BaseTool], chat_only: Iterable[str]
    ) -> ToolAccess:
        """Права из [roles.*]/[profiles.*]; опечатка в имени инструмента — отказ."""
        roles = bind(self._raw, "roles", RolesSection).root
        profiles = bind(self._raw, "profiles", ProfilesSection).root
        known = frozenset(tool.name for tool in tools)

        return ToolAccess(known, roles, profiles, chat_only, self._grant_check)

    def _require_connections(self, name: str) -> None:
        """Инструменты с соединениями пользователя работают только при [connections]."""
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


class EntryPointPlugins:
    """Обнаружение установленных tool-плагинов: entry points группы boba.tools.

    Пакет объявляет манифест в pyproject; установленный пакет виден без
    перечисления в коде, удалённый — исчезает из таблицы.
    """

    @classmethod
    def discover(cls) -> dict[str, ToolPlugin]:
        """Таблица плагинов установленных пакетов; дубликат секции — отказ."""
        table: dict[str, ToolPlugin] = {}

        for entry in entry_points(group=ToolPluginManifest.GROUP):
            manifest = entry.load()
            if not isinstance(manifest, ToolPluginManifest):
                found = type(manifest).__name__
                msg = (
                    f"entry point {entry.name!r} of group "
                    f"{ToolPluginManifest.GROUP}: expected a ToolPluginManifest, "
                    f"got {found}"
                )
                raise RuntimeError(msg)

            if manifest.section in table:
                msg = (
                    f"entry point {entry.name!r}: tool plugin section "
                    f"{manifest.section!r} is already declared by package "
                    f"{table[manifest.section].package!r}"
                )
                raise RuntimeError(msg)

            package = cls._package_of(entry)
            table[manifest.section] = cls._plugin_of(manifest, package)

        return table

    @staticmethod
    def _package_of(entry: Any) -> str:
        """Дистрибутив entry point'а; по нему ищется образ корня плагина."""
        dist = getattr(entry, "dist", None)
        if dist is None:
            msg = (
                f"entry point {entry.name!r} of group {ToolPluginManifest.GROUP} "
                "carries no distribution: the owning package is unknown"
            )
            raise RuntimeError(msg)

        return dist.name

    @classmethod
    def _plugin_of(cls, manifest: ToolPluginManifest, package: str) -> ToolPlugin:

        return ToolPlugin(
            section=manifest.section,
            build=cls._build_of(manifest),
            config_model=manifest.config_model,
            module_tools=ToolBridge.toolset(manifest.tools),
            modules=ToolBridge.modules_of(manifest.tools),
            discovered=True,
            package=package,
        )

    @staticmethod
    def _build_of(
        manifest: ToolPluginManifest,
    ) -> Callable[[Any, LauncherFactory], list[BaseTool]] | None:
        """Фабрика манифеста с мостом в langchain; манифест langchain не знает."""
        source = manifest.build
        if source is None:
            return None

        def build(cfg: Any, launchers: LauncherFactory) -> list[BaseTool]:
            built: list[BaseTool] = []
            for tool in source(cfg, launchers):
                built.append(ToolBridge.as_structured_tool(tool))

            return built

        return build


class CoreTools:
    """Таблица плагинов, общая для процессов: обнаруженные пакеты плюс
    встроенные workflow и pipeline."""

    @classmethod
    def table(cls, refs: RuntimeRefs) -> dict[str, ToolPlugin]:
        """workflow и pipeline встроены: им нужны входы приложения."""
        table = EntryPointPlugins.discover()

        table["workflow"] = ToolPlugin(
            section="workflow",
            config_model=WorkflowToolConfig,
            build=cls._workflow_builder(refs),
            sandboxed=False,
        )

        table["pipeline"] = ToolPlugin(
            section="pipeline",
            config_model=PipelineToolConfig,
            build=cls._pipeline_builder(refs),
            sandboxed=False,
        )

        table["connections"] = ToolPlugin(
            section="connections",
            config_model=ConnectionCatalogConfig,
            build=cls._connections_builder(refs),
            sandboxed=False,
        )

        return table

    @staticmethod
    def _connections_builder(
        refs: RuntimeRefs,
    ) -> Callable[[ConnectionCatalogConfig, LauncherFactory], list[BaseTool]]:
        """Каталог соединений субъекта: строки берутся из таблицы на вызов."""

        def build(
            cfg: ConnectionCatalogConfig, launchers: LauncherFactory
        ) -> list[BaseTool]:
            return build_connection_tools(
                cfg, UserConnectionsService(refs.connection_store)
            )

        return build

    @staticmethod
    def _pipeline_builder(
        refs: RuntimeRefs,
    ) -> Callable[[PipelineToolConfig, LauncherFactory], list[BaseTool]]:
        """Реестр инструментов берётся из входов приложения на каждый вызов."""

        def build(
            cfg: PipelineToolConfig, launchers: LauncherFactory
        ) -> list[BaseTool]:
            return build_pipeline_tools(cfg, refs.tool_registry)

        return build

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
