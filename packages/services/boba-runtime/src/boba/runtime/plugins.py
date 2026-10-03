"""Загрузчик tool-плагинов: секция [tool.<name>] -> langchain-инструменты с обвязками.

Плагины обнаруживаются entry points группы boba.tools у установленных пакетов;
конфиг каждого приходит файлом conf/plugins/<name>.toml (слой AppLayers).

Ошибки:
RuntimeError — конфиг противоречит плагину: у установленного плагина нет
    conf/plugins/<name>.toml, entry point отдал не манифест, секции плагинов
    совпали, способ запуска из [tool_launcher] не согласован с секциями
    (см. boba.runtime.launchers).
ToolConfigError — injected-параметр инструмента не привязан к секции конфига.
StreamGroupsConfigError — нет секции [stream_groups] со сроками групп
    потоковых вызовов.
TypeError — TOOLS модуля содержит не PayloadTool и не BaseTool; тело
    инструмента вернуло не модель результата.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from langchain_core.tools import BaseTool
from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict

from boba.access import GrantCheck, ToolAccess, ToolSurfaces
from boba.chat.profiles import ProfilesSection, RolesSection
from boba.config import bind
from boba.connection_broker.sealed import SealedConnectionParams
from boba.connection_broker.tickets import ServiceTickets
from boba.runtime.launchers import CallSurface, SectionLaunchers, ToolLaunchers
from boba.runtime.refs import RuntimeRefs
from boba.toolkit.entry import ToolArgv, ToolEntryError, ToolMain
from boba.toolkit.launcher import ToolLauncher
from boba.toolkit.manifest import LaunchSpec, ToolPluginManifest
from boba.toolkit.ports import ToolStreamSpecs
from boba.toolkit.types import StringList
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.access import ToolAccessGuard
from boba.toolrun.bridge import ToolBridge
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.cancellation import CancellableTools
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.injected import InjectedConfig, ToolConfigError
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.stream_calls import (
    StreamChannelFields,
    StreamGroupsConfig,
    StreamGroupsConfigError,
)
from boba.toolrun.streams import ToolStreams
from boba.toolrun.wrapping import CallHooks, ToolAsyncBody, ToolBody

__all__ = [
    "EntryPointPlugins",
    "PluginMeta",
    "PluginTable",
    "ToolBridge",
    "ToolLoader",
    "ToolPlugin",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolPlugin:
    """Один tool-плагин: функции модуля из секции [tool.<name>].

    Плагин приходит entry point'ом установленного пакета; его секция
    tool.<section> обязана прийти файлом conf/plugins/<section>.toml, тела
    исполняются отдельным процессом способом из [tool_launcher].
    """

    section: str
    module_tools: tuple[BaseTool, ...] = ()
    """Функции уровня модуля: обёртка запуска ставится на них."""
    modules: tuple[str, ...] = ()
    """Модули тел module_tools: их прогревает зигота секции."""
    package: str = ""
    """Дистрибутив entry point'а: по нему ищется образ корня песочницы
    plugins/<package>/rootfs.ext4."""


class PluginMeta(BaseModel):
    """Meta-конфиг плагина: что framework читает из [tool.<name>]."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    tools: StringList = []
    headless: StringList = []
    """Инструменты из tools, которые модели в чате не отдаются: их зовут
    страница, REST и workflow (снятие снимка каталога и подобные задачи)."""


class ToolLoader:
    """Сборка реестра инструментов из включённых секций [tool.<name>].

    Обвязки ставятся на копии модульных TOOLS: загрузка зовётся не один раз
    (bootstrap, DI-провайдер), а TOOLS — синглтоны процесса, и повторная
    обёртка поверх уже обёрнутого ломала бы адрес тела и схему.
    """

    def __init__(  # noqa: PLR0913 — загрузчик собирается всеми входами процесса
        self,
        raw_config: DictConfig,
        plugins: Mapping[str, ToolPlugin],
        refs: RuntimeRefs,
        grant_check: GrantCheck,
        surface_hooks: Sequence[CallHooks[Any]] = (),
        own_tools: Sequence[BaseTool] = (),
    ) -> None:
        self._raw = raw_config
        self._plugins = plugins
        self._own_tools = tuple(own_tools)
        """Собственные инструменты процесса: идут под теми же обвязками, что
        и инструменты плагинов, а за портом инструментов образуют отдельный
        сервер (ToolRegistry.server)."""
        self._credentials_ref = refs.credentials
        self._contexts = refs.contexts
        self._surface = CallSurface(refs.contexts)
        self._sealed = SealedConnectionParams(
            refs.seal_keys, refs.connection_types, refs.contexts
        )
        """Приём запечатанных соединений: обвязки параметров-соединений,
        правило аргументов узлов и возможность сервера с ключом."""
        self._grant_check = grant_check
        self._surface_hooks = tuple(surface_hooks)
        """Обвязки поверхности процесса (чат монтирует элементы результата):
        ставятся сразу после тела, до журнала и разбора ошибок."""

    def load(self) -> ToolRegistry:
        launchers = ToolLaunchers.of(self._raw)

        tools: list[BaseTool] = []
        headless_only: set[str] = set()
        for name, plugin in self._plugins.items():
            section = OmegaConf.select(self._raw, f"tool.{name}")
            if section is None:
                msg = (
                    f"conf/plugins/{name}.toml is missing: the installed "
                    f"plugin {name!r} requires its config"
                )
                raise RuntimeError(msg)

            meta = bind(self._raw, f"tool.{name}", PluginMeta)
            if not meta.enable:
                continue

            built = self._plugin_tools(plugin, meta, launchers)
            tools.extend(built)
            headless_only.update(self._headless_of(name, meta, built))

            # живой вывод есть у отдельных процессов: кнопка потока
            # рисуется на шагах инструментов
            streamable: list[str] = []
            for tool in built:
                streamable.append(tool.name)
            ToolStreams.mark_streamable(streamable)

        if next(self._stream_writers(tools), None) is not None:
            tools.append(ToolBridge.as_structured_tool(DevNullTool.build()))

        own: list[str] = []
        for tool in self._own_tools:
            tools.append(tool)
            own.append(tool.name)

        access = self._access_of(tools, headless_only)
        for hooks in self._surface_hooks:
            ToolBody.hook_all(tools, hooks)

        stream_cfg = self._stream_config()
        StreamChannelFields(stream_cfg).attach_all(tools)
        ToolCallIdField.attach_all(tools)
        ToolIntentField.attach_all(tools)
        ToolRunLogger.guard_all(
            tools, self._surface.stream_source, self._surface.tool_call_scope
        )
        CancellableTools.guard_all(tools)
        ToolAccessGuard.guard_all(tools, access, self._contexts.subject)
        ToolErrorGuard().guard_all(tools)
        ToolAsyncBody.ensure_all(tools)
        return ToolRegistry(
            tools=tools,
            access=access,
            stream_config=stream_cfg,
            own=frozenset(own),
            node_args=(self._sealed,),
        )

    @staticmethod
    def _stream_writers(tools: Sequence[BaseTool]) -> Iterator[str]:
        """Инструменты-писатели каналов: только при них слив dev_null имеет
        смысл, и модель его видит."""
        for tool in tools:
            if ToolStreamSpecs.of(tool.name).outbound():
                yield tool.name

    def _stream_config(self) -> StreamGroupsConfig:
        """Секция [stream_groups]: сроки групп и размеры пайпов каналов."""
        section = StreamGroupsConfig.SECTION
        if OmegaConf.select(self._raw, section) is None:
            msg = (
                f"config section [{section}] is missing: stream groups need "
                "open_sec, stall_sec, poll_sec, pipe_bytes and pipe_bytes_max"
            )
            raise StreamGroupsConfigError(msg)

        return bind(self._raw, section, StreamGroupsConfig)

    def _plugin_tools(
        self,
        plugin: ToolPlugin,
        meta: PluginMeta,
        launchers: SectionLaunchers,
    ) -> list[BaseTool]:
        """Инструменты плагина: функции модуля под launcher'ом секции."""
        spec = LaunchSpec(
            section=plugin.section,
            modules=plugin.modules,
            package=plugin.package,
        )
        launcher = launchers.launcher_of(spec, self._contexts)

        return self._module_tools(plugin, meta, launcher)

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
        CallContextValues.bind_all(functions, self._contexts)

        self._sealed.bind_all(functions)

        resolve = self._config_resolver()
        ServiceTickets.bind_all(functions, self._credentials_ref, resolve)
        InjectedConfig.bind_all(functions, resolve)

        return functions

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
    def _headless_of(
        name: str, meta: PluginMeta, built: Sequence[BaseTool]
    ) -> set[str]:
        """Инструменты плагина, помеченные headless: имя вне собранных — отказ."""
        known: set[str] = set()
        for tool in built:
            known.add(tool.name)

        stray = sorted(set(meta.headless) - known)
        if stray:
            msg = (
                f"[tool.{name}] headless names {stray} are not among its enabled "
                f"tools {sorted(known)}"
            )
            raise RuntimeError(msg)

        return set(meta.headless)

    def _access_of(
        self,
        tools: Sequence[BaseTool],
        headless_only: Iterable[str],
    ) -> ToolAccess:
        """Права из [roles.*]/[profiles.*]; опечатка в имени инструмента — отказ."""
        roles = bind(self._raw, "roles", RolesSection).root
        profiles = bind(self._raw, "profiles", ProfilesSection).root
        known = frozenset(tool.name for tool in tools)

        surfaces = ToolSurfaces(headless_only=frozenset(headless_only))
        return ToolAccess(known, roles, profiles, surfaces, self._grant_check)


PluginTable = Callable[[], Mapping[str, ToolPlugin]]
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
            module_tools=ToolBridge.toolset(manifest.tools),
            modules=ToolBridge.modules_of(manifest.tools),
            package=package,
        )
