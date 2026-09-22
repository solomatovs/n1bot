"""Загрузчик tool-плагинов: секция [tool.<name>] -> langchain-инструменты с обвязками.

Плагины обнаруживаются entry points группы boba.tools у установленных пакетов;
конфиг каждого приходит файлом conf/plugins/<name>.toml (слой AppLayers). Слои
конвейера вызова, общие для всех сборок, перечислены в ToolPipeline; реестр чата
(ToolLoader) добавляет к ним права и контекст пользователя, набор сервиса (ToolSet)
берёт их как есть для явного списка функций.

Ошибки:
ToolSetError — в набор сервиса подан инструмент с параметром про пользователя.
RuntimeError — конфиг противоречит плагину: у установленного плагина нет
    conf/plugins/<name>.toml, entry point отдал не манифест, секции плагинов
    совпали, способ запуска из [tool_launcher] не согласован с секциями
    (см. boba.runtime.launchers), секция с соединениями пользователя без
    [connections].
ToolConfigError — injected-параметр инструмента не привязан к секции конфига.
TypeError — TOOLS модуля содержит не PayloadTool и не BaseTool; тело
    инструмента вернуло не модель результата.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import wraps
from importlib.metadata import entry_points
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict

from boba.access import GrantCheck, ToolAccess, ToolSurfaces
from boba.chat.profiles import ProfilesSection, RolesSection
from boba.config import bind
from boba.connection_broker.store import ConnectionsConfig
from boba.connection_broker.tickets import ServiceTickets
from boba.connection_broker.user_connections import UserConnections
from boba.identity.context import CallContext
from boba.runtime.launchers import CallSurface, SectionLaunchers, ToolLaunchers
from boba.runtime.refs import RuntimeRefs
from boba.toolkit.entry import ToolAddress, ToolArgv, ToolEntryError, ToolLike, ToolMain
from boba.toolkit.facade import PayloadTool
from boba.toolkit.launcher import ToolLauncher
from boba.toolkit.manifest import LaunchSpec, ToolPluginManifest
from boba.toolkit.result import ToolResultBase
from boba.toolkit.types import StringList
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.access import ToolAccessGuard
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.callvalues import CallContextValues
from boba.toolrun.cancellation import CancellableTools
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.injected import InjectedConfig, ToolConfigError
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.run_log import ToolRunLogger
from boba.toolrun.streams import ToolStreams
from boba.toolrun.wrapping import CallHooks, ToolAsyncBody, ToolBody, ToolSchema

__all__ = [
    "CoreTools",
    "EntryPointPlugins",
    "PluginMeta",
    "PluginTable",
    "ToolBridge",
    "ToolLoader",
    "ToolPipeline",
    "ToolPlugin",
    "ToolSet",
    "ToolSetError",
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


class ToolBridge:
    """Мост TOOLS модулей инструментов в langchain: toolkit langchain не знает."""

    @classmethod
    def as_structured_tool(cls, tool: ToolLike) -> BaseTool:
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

        func = None
        if tool.func is not None:
            func = cls._packed(tool, tool.func)

        coroutine = None
        if tool.coroutine is not None:
            coroutine = cls._packed_async(tool, tool.coroutine)

        return StructuredTool(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            func=func,
            coroutine=coroutine,
            response_format=PayloadTool.RESPONSE_FORMAT,
        )

    @classmethod
    def _packed(
        cls, tool: PayloadTool, body: Callable[..., Any]
    ) -> Callable[..., tuple[str, ToolResultBase]]:
        """Тело, отдающее модель, -> тело с парой (content, artifact) langchain.

        Аргументы langchain приходят по отдельности: тело с классом вызова
        получает их его экземпляром. wraps сохраняет исходное тело в
        __wrapped__: адрес запуска и каталог workflow читают оттуда модуль
        и аннотацию результата.
        """

        @wraps(body)
        def call(**kwargs: Any) -> tuple[str, ToolResultBase]:
            return cls._pack(tool.name, body(**tool.packed_kwargs(kwargs)))

        return call

    @classmethod
    def _packed_async(
        cls, tool: PayloadTool, body: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[tuple[str, ToolResultBase]]]:
        @wraps(body)
        async def call(**kwargs: Any) -> tuple[str, ToolResultBase]:
            return cls._pack(tool.name, await body(**tool.packed_kwargs(kwargs)))

        return call

    @staticmethod
    def _pack(name: str, result: object) -> tuple[str, ToolResultBase]:
        if not isinstance(result, ToolResultBase):
            msg = (
                f"tool {name!r} must return a ToolResultBase model, "
                f"got {type(result).__name__}"
            )
            raise TypeError(msg)

        return result.packed()

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


class ToolPipeline:
    """Общие слои конвейера вызова, одни на все сборки инструментов.

    Конвейер состоит из двух половин. Эта — без пользователя: запуск тела отдельным
    процессом или песочницей, id вызова, поле намерения, журнал вызова, отмена,
    перевод исключения в результат и приведение тела к async. Половину про
    пользователя — права по ролям, значения контекста вызова, соединения и билеты —
    добавляет ToolLoader между instrument и seal. Слои перечислены здесь один раз,
    поэтому реестр чата и набор сервиса не расходятся.
    """

    @staticmethod
    def launch(functions: Sequence[BaseTool], launcher: ToolLauncher) -> None:
        """Тело исполняется запускателем: субпроцесс хоста или зигота песочницы."""
        ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

    @staticmethod
    def instrument(tools: Sequence[BaseTool]) -> None:
        """Слои наблюдения вокруг вызова: id, намерение, журнал, отмена."""
        ToolCallIdField.attach_all(tools)
        ToolIntentField.attach_all(tools)
        ToolRunLogger.guard_all(
            tools, CallSurface.stream_source, CallSurface.tool_call_scope
        )
        CancellableTools.guard_all(tools)

    @staticmethod
    def seal(tools: Sequence[BaseTool]) -> None:
        """Внешние слои: исключение становится результатом, тело всегда async."""
        ToolErrorGuard.guard_all(tools)
        ToolAsyncBody.ensure_all(tools)


class ToolSetError(Exception):
    """Набор инструментов сервиса не собирается."""


class ToolSet:
    """Инструменты сервиса: явный список функций через общий конвейер.

    Для процесса без пользователя — индексатора, описателя, планировщика — реестр с
    ролями, профилями и плагинами избыточен, а обходить конвейер нельзя. Набор берёт
    функции с @tool, значения их Injected-параметров готовыми моделями и запускатель,
    и проводит их теми же слоями ToolPipeline, что и реестр: тело исполняется
    запускателем, ошибки становятся результатом, вызов ведёт журнал.

    Параметров про пользователя здесь нет: инструмент с Subject, Scope, корнем
    workspace или соединением субъекта в набор не проходит — ему нужен человек, а
    значит реестр и контекст вызова.
    """

    def __init__(self, launcher: ToolLauncher, injected: Sequence[BaseModel]) -> None:
        self._launcher = launcher
        values: dict[type, BaseModel] = {}
        for value in injected:
            values[type(value)] = value

        self._injected = values

    def build(self, functions: Sequence[ToolLike]) -> list[BaseTool]:
        tools: list[BaseTool] = []
        for function in functions:
            tools.append(ToolBridge.as_structured_tool(function).model_copy())

        for tool in tools:
            self._refuse_user_bound(tool)

        ToolPipeline.launch(tools, self._launcher)
        InjectedConfig.bind_all(tools, self._resolve)
        ToolPipeline.instrument(tools)
        ToolPipeline.seal(tools)

        return tools

    def _resolve(self, param: str, annotation: Any) -> object:
        value = self._injected.get(annotation)
        if value is None:
            known: list[str] = []
            for model in self._injected:
                known.append(model.__name__)

            raise ToolConfigError(
                f"injected parameter {param!r} expects {annotation!r}, but the tool "
                f"set was given values only for {known}"
            )

        return value

    @staticmethod
    def _refuse_user_bound(tool: BaseTool) -> None:
        schema = ToolSchema.of(tool)
        if schema is None:
            return

        connections = ToolArgv.connection_fields(schema)
        if connections:
            raise ToolSetError(
                f"tool {tool.name}: parameters {sorted(connections)} are user "
                "connections; the tool needs a subject and belongs in the registry, "
                "not in a service tool set"
            )

        for name, annotation in ToolArgv.injected_fields(schema).items():
            if annotation in CallContextValues.SOURCES:
                kind = annotation.__name__
                raise ToolSetError(
                    f"tool {tool.name}: parameter {name!r} is the {kind} of a call "
                    "context; the tool needs a subject and belongs in the registry, "
                    "not in a service tool set"
                )


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
        surface_hooks: Sequence[CallHooks[Any]] = (),
    ) -> None:
        self._raw = raw_config
        self._plugins = plugins
        self._store_ref = refs.connection_store
        self._credentials_ref = refs.credentials
        self._types_ref = refs.connection_types
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

        access = self._access_of(tools, headless_only)
        for hooks in self._surface_hooks:
            ToolBody.hook_all(tools, hooks)

        ToolPipeline.instrument(tools)
        ToolAccessGuard.guard_all(tools, access, CallContext.current_subject)
        ToolPipeline.seal(tools)

        return ToolRegistry(tools=tools, access=access)

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
        launcher = launchers.launcher_of(spec)

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

        ToolPipeline.launch(functions, launcher)
        CallContextValues.bind_all(functions)

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


class CoreTools:
    """Таблица плагинов, общая для процессов: обнаруженные пакеты."""

    @staticmethod
    def table() -> dict[str, ToolPlugin]:
        """Плагины установленных пакетов: все инструменты приходят entry point'ами."""
        return EntryPointPlugins.discover()
