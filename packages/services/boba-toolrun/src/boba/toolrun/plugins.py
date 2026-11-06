"""Загрузка манифестов плагинов и сборка исполнителей через переданные порты.

Ошибки:
ConfigError — секция плагина не соответствует конфигурации.
RuntimeError — запуск или манифест плагина не согласован с запросом.
StreamGroupsConfigError — настройки потоков отсутствуют или некорректны."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict

from boba.access import ToolAccess, ToolGrants, ToolPolicy
from boba.config import ConfigError, ConfigFileSections, bind
from boba.core.types import StringList
from boba.identity.context import CallContexts
from boba.tool_io.chain import CallAmbient
from boba.toolkit.manifest import ToolDecl
from boba.toolkit.ports import StreamSpecs
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.hosted import HostedTool, PluginSection, ToolHosting
from boba.toolrun.injected import ParamSource
from boba.toolrun.launchers import PluginLaunch, SectionLaunchers
from boba.toolrun.registry import ToolChain, ToolRegistry
from boba.toolrun.stream_calls import (
    NodeArgs,
    StreamGroupsConfig,
    StreamGroupsConfigError,
)
from boba.toolrun.streams import CallJournals


class PluginMeta(BaseModel):
    """Ключи фреймворка в [tool.<id>]: включён ли плагин и какие инструменты
    он предоставляет. Остальные ключи секции проверяет плагин подкомандой manifest."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    tools: StringList = []
    migration_version: str | None = None


class PluginSections:
    """Секции [tool.<id>] конфига процесса: какие плагины названы и значения
    секции плагина JSON-ом, как их получают подкоманды программы и тело.

    Создают его комплекты плагинов (PluginKits), отладочный запуск тела
    (toolcli) и стенды тестов — у всех одна сырая секция, без моделей
    плагина.
    """

    SECTION: ClassVar[str] = "tool"
    FILE_SECTIONS: ClassVar[tuple[ConfigFileSections, ...]] = (
        ConfigFileSections("plugins", SECTION),
    )

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

    Создаёт его провайдер процесса со способом запуска. load() получает
    манифесты включённых плагинов от их программ по очереди. Таблицы
    подготавливает отдельная команда migrate. Ошибка включённого плагина
    останавливает сборку процесса с именем плагина и причиной.
    """

    def __init__(self, raw: DictConfig, launchers: SectionLaunchers) -> None:
        self._sections = PluginSections(raw)
        self._launchers = launchers

    async def load(self) -> dict[str, PluginKit]:
        kits: dict[str, PluginKit] = {}
        for plugin_id in self._sections.ids():
            meta = self._sections.meta(plugin_id)
            if not meta.enable:
                continue

            values = self._sections.values(plugin_id)
            kit = await self._start(plugin_id, meta, values)
            kits[kit.launch.id] = kit

        return kits

    async def _start(
        self, plugin_id: str, meta: PluginMeta, values: dict[str, Any]
    ) -> PluginKit:
        """Манифест одного включённого плагина; отказ прерывает сборку."""
        try:
            manifest = await asyncio.to_thread(
                self._launchers.manifest_of, plugin_id, values
            )
            launch = PluginLaunch(manifest=manifest, section=values)
            section = PluginSection(
                values=values, connections=manifest.connections_in_config
            )
        except (RuntimeError, ConfigError) as exc:
            raise ConfigError(f"loading plugin {plugin_id!r} failed: {exc}") from exc

        return PluginKit(launch=launch, meta=meta, section=section)


@dataclass(frozen=True)
class ToolExecution:
    """Зависимости исполнения, переданные приложением загрузчику плагинов."""

    contexts: CallContexts
    journals: CallJournals
    ambient: CallAmbient
    parameters: Sequence[ParamSource] = ()
    node_args: Sequence[NodeArgs] = ()


class ToolLoader:
    """Сборка реестра инструментов из включённых плагинов процесса.

    Инструменты хоста собираются из объявлений манифестов заново на каждую
    загрузку (bootstrap, DI-провайдер): обвязки ложатся на свежие объекты.
    """

    def __init__(  # noqa: PLR0913 — загрузчик собирается всеми входами процесса
        self,
        raw_config: DictConfig,
        kits: Mapping[str, PluginKit],
        execution: ToolExecution,
        launchers: SectionLaunchers,
        grants: ToolGrants | ToolPolicy,
        own_tools: Sequence[HostedTool] = (),
    ) -> None:
        self._raw = raw_config
        self._sections = PluginSections(raw_config)
        self._launchers = launchers
        """Способ запуска процесса: один на все сборки инструментов."""
        self._kits = kits
        self._own_tools = tuple(own_tools)
        """Собственные инструменты процесса: идут под теми же обвязками, что
        и инструменты плагинов, а за портом инструментов образуют отдельный
        сервер (ToolRegistry.server)."""
        self._contexts = execution.contexts
        self._journals = execution.journals
        self._stream_cfg = self._stream_config()
        self._ambient = execution.ambient
        self._drain = DevNullTool(execution.ambient)
        self._hosting = ToolHosting()
        self._node_args = execution.node_args
        self._chain = ToolChain(
            self._stream_cfg,
            execution.journals,
            execution.contexts,
            execution.ambient,
            (CallContextValues(execution.contexts), *execution.parameters),
        )
        self._grants = grants

    def load(self) -> ToolRegistry:
        tools: list[HostedTool] = []
        specs = StreamSpecs({})
        for kit in self._kits.values():
            launcher = self._launchers.launcher_of(
                kit.launch, self._contexts, self._ambient
            )
            plugged = self._chain.launch(self._enabled(kit), launcher, kit.section)
            built = plugged.tools
            tools.extend(built)
            specs = specs.merged(plugged.specs)
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
            node_args=self._node_args,
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

    def _access_of(self, tools: Sequence[HostedTool]) -> ToolPolicy:
        """Использует политику приложения либо собирает статические гранты стенда."""
        if not isinstance(self._grants, ToolGrants):
            return self._grants

        known: set[str] = set()
        for tool in tools:
            known.add(tool.name)

        grants = self._grants

        return ToolAccess(
            frozenset(known),
            grants.roles,
            grants.profiles,
            grants.check,
        )
