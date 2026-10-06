"""Загрузчик tool-плагинов: секция [tool.<name>] -> инструменты хоста с обвязками.

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
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict

from boba.access import GrantCheck, ToolAccess, ToolGrants, ToolSurfaces
from boba.chat.profiles import ProfilesSection, RolesSection
from boba.config import bind
from boba.connection_broker.sealed import SealedConnectionParams
from boba.connection_broker.tickets import ServiceTickets
from boba.runtime.launchers import SectionLaunchers
from boba.runtime.refs import ExecRefs
from boba.toolkit.entry import ToolArgv, ToolEntryError
from boba.toolkit.facade import PayloadTool
from boba.toolkit.launcher import ToolLauncher
from boba.toolkit.manifest import LaunchSpec, ToolPluginManifest
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.types import StringList
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.hosted import HostedTool, ToolHosting
from boba.toolrun.injected import InjectedConfig, ToolConfigError
from boba.toolrun.registry import LaunchedTools, ToolChain, ToolRegistry
from boba.toolrun.stream_calls import (
    StreamGroupsConfig,
    StreamGroupsConfigError,
)

__all__ = [
    "ConfigGrants",
    "EntryPointPlugins",
    "PluginMeta",
    "PluginTable",
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
    module_tools: tuple[PayloadTool, ...] = ()
    """Объявления инструментов модуля: из них обёртка запуска собирает
    инструменты хоста."""
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

    Инструменты хоста собираются из объявлений TOOLS заново на каждую
    загрузку (bootstrap, DI-провайдер): объявления — синглтоны процесса, и
    обвязки на них не ложатся.
    """

    def __init__(  # noqa: PLR0913 — загрузчик собирается всеми входами процесса
        self,
        raw_config: DictConfig,
        plugins: Mapping[str, ToolPlugin],
        refs: ExecRefs,
        launchers: SectionLaunchers,
        grants: ToolGrants,
        own_tools: Sequence[HostedTool] = (),
    ) -> None:
        self._raw = raw_config
        self._launchers = launchers
        """Способ запуска процесса: один на все сборки инструментов."""
        self._plugins = plugins
        self._own_tools = tuple(own_tools)
        """Собственные инструменты процесса: идут под теми же обвязками, что
        и инструменты плагинов, а за портом инструментов образуют отдельный
        сервер (ToolRegistry.server)."""
        self._contexts = refs.contexts
        self._journals = refs.journals
        self._stream_cfg = self._stream_config()
        self._ambient = refs.ambient
        self._drain = DevNullTool(refs.ambient)
        self._hosting = ToolHosting()
        self._sealed = SealedConnectionParams(
            refs.seal_keys, refs.connection_types, refs.contexts
        )
        """Приём запечатанных соединений: обвязки параметров-соединений,
        правило аргументов узлов и возможность сервера с ключом."""
        self._chain = ToolChain(
            self._stream_cfg,
            refs.journals,
            refs.contexts,
            refs.ambient,
            (
                CallContextValues(refs.contexts),
                self._sealed,
                InjectedConfig(
                    self._config_resolver(), ServiceTickets(refs.credentials)
                ),
            ),
        )
        self._grants = grants

    def load(self) -> ToolRegistry:

        tools: list[HostedTool] = []
        specs = StreamSpecs({})
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

            plugged = self._plugin_tools(plugin, meta, self._launchers)
            built = plugged.tools
            tools.extend(built)
            specs = specs.merged(plugged.specs)
            headless_only.update(self._headless_of(name, meta, built))

            # живой вывод есть у отдельных процессов: кнопка потока
            # рисуется на шагах инструментов
            streamable: list[str] = []
            for tool in built:
                streamable.append(tool.name)
            self._journals.mark_streamable(streamable)

        if next(self._stream_writers(tools, specs), None) is not None:
            tools.append(self._hosting.hosted(self._drain.build()))
            specs = specs.declaring(DevNullTool.NAME, self._drain.spec())

        own: list[str] = []
        for tool in self._own_tools:
            tools.append(tool)
            own.append(tool.name)

        access = self._access_of(tools, headless_only)
        self._chain.seal(tools, access, specs)
        return ToolRegistry(
            tools=tools,
            access=access,
            stream_config=self._stream_cfg,
            own=frozenset(own),
            node_args=(self._sealed,),
            specs=specs,
            ambient=self._ambient,
        )

    @staticmethod
    def _stream_writers(
        tools: Sequence[HostedTool], specs: StreamSpecs
    ) -> Iterator[str]:
        """Инструменты-писатели каналов: только при них слив dev_null имеет
        смысл, и модель его видит."""
        for tool in tools:
            if specs.of(tool.name).outbound():
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
    ) -> LaunchedTools:
        """Инструменты плагина: функции модуля под launcher'ом секции."""
        spec = LaunchSpec(
            section=plugin.section,
            modules=plugin.modules,
            package=plugin.package,
        )
        launcher = launchers.launcher_of(spec, self._contexts, self._ambient)

        return self._module_tools(plugin, meta, launcher)

    def _module_tools(
        self,
        plugin: ToolPlugin,
        meta: PluginMeta,
        launcher: ToolLauncher,
    ) -> LaunchedTools:
        """Функции модуля новой модели: обёртка запуска + partial конфига."""
        enabled: list[PayloadTool] = []
        for tool in plugin.module_tools:
            if tool.name not in meta.tools:
                continue

            enabled.append(tool)

        return self._chain.launch(enabled, launcher)

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
        name: str, meta: PluginMeta, built: Sequence[HostedTool]
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
        tools: Sequence[HostedTool],
        headless_only: Iterable[str],
    ) -> ToolAccess:
        """Права по грантам процесса; опечатка в имени инструмента — отказ."""
        known = frozenset(tool.name for tool in tools)
        grants = self._grants

        surfaces = ToolSurfaces(headless_only=frozenset(headless_only))
        return ToolAccess(known, grants.roles, grants.profiles, surfaces, grants.check)


class ConfigGrants:
    """Гранты приложения с профилями чата: секции [roles.*] и [profiles.*].

    Создаётся сборкой приложения (провайдер tool_grants, загрузчик чата) из
    сырого конфига; приложение с другим источником грантов собирает
    ToolGrants само.
    """

    def __init__(self, raw_config: DictConfig) -> None:
        self._raw = raw_config

    def grants(self, check: GrantCheck) -> ToolGrants:
        roles = bind(self._raw, "roles", RolesSection).root
        profiles = bind(self._raw, "profiles", ProfilesSection).root

        return ToolGrants(roles=roles, profiles=profiles, check=check)


PluginTable = Callable[[], Mapping[str, ToolPlugin]]
"""Таблица плагинов процесса: общая часть плюс своё (у чата — chat-only инструменты)."""


class EntryPointPlugins:
    """Обнаружение установленных tool-плагинов: entry points группы boba.tools.

    Пакет объявляет манифест в pyproject; установленный пакет виден без
    перечисления в коде, удалённый — исчезает из таблицы.
    """

    def __init__(self) -> None:
        self._hosting = ToolHosting()

    def discover(self) -> dict[str, ToolPlugin]:
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

            package = self._package_of(entry)
            table[manifest.section] = self._plugin_of(manifest, package)

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

    def _plugin_of(self, manifest: ToolPluginManifest, package: str) -> ToolPlugin:
        return ToolPlugin(
            section=manifest.section,
            module_tools=manifest.tools,
            modules=self._hosting.modules_of(manifest.tools),
            package=package,
        )
