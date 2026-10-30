"""Плагины инструментов процесса: комплекты, манифесты и инструменты хоста.

Плагин процесса — секция [tool.<id>] из файла plugins/<id>.toml, который
конфиг экземпляра подключает ключом tool = ${toml_dir:…}, и программа плагина, которую исполняет способ запуска
(boba.runtime.launchers). Сервер кода плагинов не импортирует: манифест
он получает от программы данными, хранилище готовит её подкомандой setup,
инструменты хоста собирает из объявлений манифеста.

Ошибки:
RuntimeError — конфиг противоречит плагину: комплект неполон, программа не
    отдала манифест или не подготовила хранилище, список инструментов
    секции называет то, чего плагин не объявил, способ запуска из
    [tool_launcher] не согласован с секциями (см. boba.runtime.launchers).
ToolConfigError — служебному параметру инструмента нечего подставить либо
    вид соединения из манифеста серверу неизвестен.
StreamGroupsConfigError — нет секции [stream_groups] со сроками групп
    потоковых вызовов.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict

from boba.access import GrantCheck, ToolAccess, ToolGrants
from boba.chat.profiles import ProfilesSection, RolesSection
from boba.config import bind
from boba.connection_broker.tickets import SectionTickets, ServiceTickets
from boba.runtime.launchers import PluginLaunch, SectionLaunchers
from boba.runtime.refs import ExecRefs
from boba.toolkit.manifest import PluginManifest, ToolDecl
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.types import StringList
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.hosted import HostedTool, PluginSection, ToolHosting
from boba.toolrun.injected import InjectedConfig, SectionArming
from boba.toolrun.registry import ToolChain, ToolRegistry
from boba.toolrun.stream_calls import (
    StreamGroupsConfig,
    StreamGroupsConfigError,
)

__all__ = [
    "ConfigGrants",
    "PluginKit",
    "PluginKits",
    "PluginMeta",
    "PluginSections",
    "ToolLoader",
]

logger = logging.getLogger(__name__)


class PluginMeta(BaseModel):
    """Ключи фреймворка в [tool.<id>]: включён ли плагин, его инструменты и
    заданные соединения сервера. Остальные ключи секции проверяет сам
    плагин подкомандой manifest."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    tools: StringList = []
    connections: dict[str, StringList] = {}
    """Списки заданных соединений сервера у инструментов плагина: имя
    инструмента → имена из [connections]."""


class PluginSections:
    """Секции [tool.<id>] конфига процесса: какие плагины названы и значения
    секции плагина JSON-ом, как их получают подкоманды программы и тело.

    Создают его комплекты плагинов (PluginKits), отладочный запуск тела
    (toolcli) и стенды тестов — у всех одна сырая секция, без моделей
    плагина.
    """

    SECTION: ClassVar[str] = "tool"

    def __init__(self, raw: DictConfig) -> None:
        self._raw = raw

    def ids(self) -> Iterator[str]:
        """Плагины, названные секциями [tool.<id>]."""
        node = OmegaConf.select(self._raw, self.SECTION)
        if node is None:
            return

        for key in node:
            yield str(key)

    def meta(self, plugin_id: str) -> PluginMeta:
        """Ключи фреймворка секции плагина."""
        return bind(self._raw, self._path(plugin_id), PluginMeta)

    def values(self, plugin_id: str) -> dict[str, Any]:
        """Секция плагина значениями JSON с развёрнутыми интерполяциями.

        Ошибки:
        RuntimeError — секции [tool.<id>] в конфиге нет.
        """
        node = OmegaConf.select(self._raw, self._path(plugin_id))
        if node is None:
            msg = f"the config has no [{self._path(plugin_id)}] section"
            raise RuntimeError(msg)

        container = OmegaConf.to_container(node, resolve=True)
        values: dict[str, Any] = {}
        if isinstance(container, dict):
            for key, value in container.items():
                values[str(key)] = value

        return values

    def _path(self, plugin_id: str) -> str:
        return f"{self.SECTION}.{plugin_id}"


@dataclass(frozen=True)
class PluginKit:
    """Включённый плагин после старта: что запускать (манифест и секция),
    ключи фреймворка и секция с профилями соединений для инструментов."""

    launch: PluginLaunch
    meta: PluginMeta
    section: PluginSection


class PluginKits:
    """Комплекты плагинов процесса из его конфига.

    Создаёт его провайдер процесса со способом запуска и довооружением
    секций. load() проверяет полноту комплектов, получает манифесты
    включённых плагинов от их программ одновременно и готовит хранилища
    тех, чей манифест объявил setup: секция уезжает подкоманде с билетами
    вместо keytab-профилей.
    """

    def __init__(
        self, raw: DictConfig, launchers: SectionLaunchers, arming: SectionArming
    ) -> None:
        self._sections = PluginSections(raw)
        self._launchers = launchers
        self._arming = arming

    async def load(self) -> dict[str, PluginKit]:
        ids = list(self._sections.ids())
        self._launchers.check_kits(ids)

        enabled: list[tuple[str, PluginMeta, dict[str, Any]]] = []
        for plugin_id in ids:
            meta = self._sections.meta(plugin_id)
            if not meta.enable:
                continue

            enabled.append((plugin_id, meta, self._sections.values(plugin_id)))

        manifests = await asyncio.gather(*self._manifests(enabled))

        kits: dict[str, PluginKit] = {}
        for (plugin_id, meta, values), manifest in zip(enabled, manifests, strict=True):
            launch = PluginLaunch(manifest=manifest, section=values)
            section = PluginSection(
                values=values, connections=manifest.connections_in_config
            )
            await self._set_up(launch, section)
            kits[plugin_id] = PluginKit(launch=launch, meta=meta, section=section)

        return kits

    def _manifests(
        self, enabled: Sequence[tuple[str, PluginMeta, dict[str, Any]]]
    ) -> Iterator[Coroutine[Any, Any, PluginManifest]]:
        for plugin_id, _meta, values in enabled:
            yield asyncio.to_thread(self._launchers.manifest_of, plugin_id, values)

    async def _set_up(self, launch: PluginLaunch, section: PluginSection) -> None:
        if not launch.manifest.setup:
            return

        armed = await self._arming.armed(launch.id, section)
        await asyncio.to_thread(self._launchers.setup, launch, armed)
        logger.info("plugin %s: storage is ready", launch.id)


class ToolLoader:
    """Сборка реестра инструментов из включённых плагинов процесса.

    Инструменты хоста собираются из объявлений манифестов заново на каждую
    загрузку (bootstrap, DI-провайдер): обвязки ложатся на свежие объекты.
    """

    def __init__(  # noqa: PLR0913 — загрузчик собирается всеми входами процесса
        self,
        raw_config: DictConfig,
        kits: Mapping[str, PluginKit],
        refs: ExecRefs,
        launchers: SectionLaunchers,
        grants: ToolGrants,
        own_tools: Sequence[HostedTool] = (),
    ) -> None:
        self._raw = raw_config
        self._launchers = launchers
        """Способ запуска процесса: один на все сборки инструментов."""
        self._kits = kits
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
        self._sealed = refs.connection_params
        """Соединения инструментов: обвязки параметров-соединений, правило
        аргументов узлов и возможность сервера с ключом."""
        tickets = SectionTickets(
            ServiceTickets(refs.credentials), refs.connection_types
        )
        self._chain = ToolChain(
            self._stream_cfg,
            refs.journals,
            refs.contexts,
            refs.ambient,
            (CallContextValues(refs.contexts), self._sealed, InjectedConfig(tickets)),
        )
        self._grants = grants

    def load(self) -> ToolRegistry:
        tools: list[HostedTool] = []
        specs = StreamSpecs({})
        for plugin_id, kit in self._kits.items():
            meta = kit.meta
            self._sealed.listed(meta.connections)

            launcher = self._launchers.launcher_of(
                kit.launch, self._contexts, self._ambient
            )
            plugged = self._chain.launch(self._enabled(kit), launcher, kit.section)
            built = plugged.tools
            tools.extend(built)
            specs = specs.merged(plugged.specs)
            self._listed(plugin_id, "connections", meta.connections, built)

            # живой вывод есть у отдельных процессов: кнопка потока
            # рисуется на шагах инструментов
            streamable: list[str] = []
            for tool in built:
                streamable.append(tool.name)
            self._journals.mark_streamable(streamable)

        self._launchers.settle()

        if next(self._stream_writers(tools, specs), None) is not None:
            tools.append(self._hosting.hosted(self._drain.build()))
            specs = specs.declaring(DevNullTool.NAME, self._drain.spec())

        own: list[str] = []
        for tool in self._own_tools:
            tools.append(tool)
            own.append(tool.name)

        access = self._access_of(tools)
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
    def _enabled(kit: PluginKit) -> list[ToolDecl]:
        """Объявления инструментов из списка tools секции; имя, которого
        плагин не объявил, — отказ."""
        declared: dict[str, ToolDecl] = {}
        for decl in kit.launch.manifest.tools:
            declared[decl.name] = decl

        stray = sorted(set(kit.meta.tools) - set(declared))
        if stray:
            msg = (
                f"[tool.{kit.launch.id}] tools names {stray}, which plugin "
                f"{kit.launch.id!r} does not declare: it has {sorted(declared)}"
            )
            raise RuntimeError(msg)

        enabled: list[ToolDecl] = []
        for name, decl in declared.items():
            if name in kit.meta.tools:
                enabled.append(decl)

        return enabled

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

    @staticmethod
    def _listed(
        name: str, key: str, listed: Iterable[str], built: Sequence[HostedTool]
    ) -> set[str]:
        """Инструменты плагина name, названные в его списке key: имя вне
        собранных — отказ."""
        known: set[str] = set()
        for tool in built:
            known.add(tool.name)

        stray = sorted(set(listed) - known)
        if stray:
            msg = (
                f"[tool.{name}] {key} names {stray} are not among its enabled "
                f"tools {sorted(known)}"
            )
            raise RuntimeError(msg)

        return set(listed)

    def _access_of(self, tools: Sequence[HostedTool]) -> ToolAccess:
        """Права по грантам процесса; опечатка в имени инструмента — отказ."""
        known = frozenset(tool.name for tool in tools)
        grants = self._grants

        return ToolAccess(known, grants.roles, grants.profiles, grants.check)


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
