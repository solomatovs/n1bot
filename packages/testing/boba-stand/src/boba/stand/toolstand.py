"""Общий стенд инструментов: пробные инструменты и реестр под боевой
цепочкой обвязок.

ProbeTools — пробные инструменты хоста с телом в процессе теста: над телом,
которое пишет тест, и регистратор, чьё тело запоминает полученные аргументы;
он же исполняет один вызов узла инструментом. ToolStand собирает инструменты
тем же путём, что боевой загрузчик (ToolLoader): инструмент хоста под
обёрткой запуска из объявления фасада и источники параметров секции
(ToolChain.launch), слив dev_null при писателях каналов, обвязки
(ToolChain.seal) и реестр ToolRegistry, из которого берётся порт ToolServer.

Ошибки:
AssertionError — у пробы спросили аргументы вызова, которого не было.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import ClassVar, TypeAlias

from pydantic import BaseModel, JsonValue

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.identity.context import CallContexts
from boba.toolkit.chain import CallAmbient
from boba.toolkit.dag import DagNode, NodeCalls, NodeOutcome, ToolServer
from boba.toolkit.facade import PayloadTool
from boba.toolkit.launcher import ToolLauncher
from boba.toolkit.ports import StreamSpecs
from boba.toolkit.result import MarkdownResult, ToolResultBase
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.hosted import HostedCall, HostedTool, ToolHosting
from boba.toolrun.injected import ParamSource
from boba.toolrun.process import ProcessLauncherConfig, ProcessToolCaller
from boba.toolrun.registry import ToolChain, ToolRegistry
from boba.toolrun.stream_calls import NodeArgs, StreamGroupsConfig
from boba.toolrun.streams import CallJournals

__all__ = ["ProbeBody", "ProbeTools", "ToolStand"]

ProbeBody: TypeAlias = Callable[..., Awaitable[ToolResultBase]]
"""Тело пробного инструмента: аргументы схемы по именам, итог — результат."""


class ProbeTools:
    """Пробные инструменты хоста с телом в процессе теста и их вызов.

    Тесту обвязки нужен инструмент, который ничего не запускает: тело либо
    пишет сам тест (hosted), либо оно только запоминает, с какими аргументами
    его позвали (recorder), — по ним тест проверяет, что обвязка отдала телу.
    Создаётся тестом или его стендом; готовые инструменты уходят под обвязки
    (ToolChain, отдельные guard_all, источники параметров) либо в ToolStand.
    call() исполняет один вызов узла самим инструментом, без порта.
    """

    TAKEN: ClassVar[str] = "taken"
    """Текст результата регистратора."""

    def __init__(self) -> None:
        self._received: dict[str, list[Mapping[str, object]]] = {}

    def hosted(self, name: str, schema: type[BaseModel], body: ProbeBody) -> HostedTool:
        """Инструмент хоста name со схемой schema над телом body."""
        return HostedTool(
            name=name,
            description=name,
            args_schema=schema,
            views=None,
            body=partial(self._called, body),
        )

    def recorder(self, name: str, schema: type[BaseModel]) -> HostedTool:
        """Инструмент, чьё тело запоминает полученные аргументы как есть."""
        return self.hosted(name, schema, partial(self._recorded, name))

    def received(self, name: str) -> Sequence[Mapping[str, object]]:
        """Аргументы, с которыми звали тело регистратора name, по вызовам."""
        return tuple(self._received.get(name, ()))

    def last(self, name: str) -> Mapping[str, object]:
        """Аргументы последнего вызова тела регистратора name."""
        received = self._received.get(name)
        if not received:
            msg = (
                f"probe tool {name!r}: the body was expected to be called, but "
                f"it has not been; called probes: {sorted(self._received)}"
            )
            raise AssertionError(msg)

        return received[-1]

    async def call(
        self, tool: HostedTool, key: str, args: Mapping[str, JsonValue]
    ) -> NodeOutcome:
        """Один вызов узла с идентификатором key самим инструментом tool."""
        return await tool.call(DagNode(key=key, tool=tool.name, args=args))

    @staticmethod
    async def _called(body: ProbeBody, call: HostedCall) -> ToolResultBase:
        return await body(**call.kwargs)

    async def _recorded(self, tool: str, /, **kwargs: object) -> ToolResultBase:
        self._received.setdefault(tool, []).append(kwargs)

        return MarkdownResult(text=self.TAKEN)


class ToolStand:
    """Реестр инструментов стенда, собранный цепочкой обвязок ToolChain.

    Тесты и стенды исполнения собирают инструменты здесь, а не своей
    последовательностью обёрток: порядок постановки тот же, что у боевого
    загрузчика ToolLoader, и держит его один ToolChain. Создаётся тестом из
    секции [stream_groups], держателя контекста вызова, журналов, обстановки
    вызова и источников служебных параметров тела. launch() ставит обёртку
    запуска и источники на инструменты одной секции — их уже можно звать
    напрямую; host() берёт инструменты с телом в процессе теста, как
    собственные инструменты приложения; adopt() — готовые инструменты хоста
    (пробы ProbeTools). registry() ставит обвязки на всё собранное и отдаёт
    ToolRegistry; server() — порт ToolServer над ним с путём вызова узла
    NodeCalls, который задаёт тест.
    """

    CHANNEL_LIMIT_BYTES: ClassVar[int] = 8_000_000
    STDERR_TAIL_BYTES: ClassVar[int] = 16384
    KILL_GRACE_SEC: ClassVar[float] = 0.5

    def __init__(
        self,
        stream: StreamGroupsConfig,
        contexts: CallContexts,
        journals: CallJournals,
        ambient: CallAmbient,
        sources: Sequence[ParamSource],
    ) -> None:
        self._stream = stream
        self._contexts = contexts
        self._journals = journals
        self._ambient = ambient
        self._chain = ToolChain(stream, journals, contexts, ambient, sources)
        self._hosting = ToolHosting()
        self._drain = DevNullTool(ambient)
        self._tools: list[HostedTool] = []
        self._specs = StreamSpecs({})

    def process_launcher(
        self, section: str, workdir: Path, timeout_sec: float
    ) -> ProcessToolCaller:
        """Запуск тел субпроцессом хоста в каталоге workdir: исполнитель
        секции section со сроком вызова timeout_sec."""
        config = ProcessLauncherConfig(
            provider="process",
            workdir=str(workdir),
            timeout_sec=timeout_sec,
            channel_limit_bytes=self.CHANNEL_LIMIT_BYTES,
            stderr_tail_bytes=self.STDERR_TAIL_BYTES,
            kill_grace_sec=self.KILL_GRACE_SEC,
        )

        return ProcessToolCaller(section, config, self._contexts, self._ambient)

    def launch(
        self, payloads: Sequence[PayloadTool], launcher: ToolLauncher
    ) -> list[HostedTool]:
        """Инструменты одной секции под обёрткой запуска launcher и
        источниками служебных параметров стенда; у каждого есть живой вывод.
        С первым писателем каналов к инструментам стенда добавляется слив
        dev_null, как в загрузчике."""
        launched = self._chain.launch(payloads, launcher)
        tools = launched.tools
        specs = launched.specs

        names: list[str] = []
        for tool in tools:
            names.append(tool.name)

        self._journals.mark_streamable(names)
        self._tools.extend(tools)
        self._specs = self._specs.merged(specs)
        self._drained(tools, specs)

        return list(tools)

    def host(self, payloads: Sequence[PayloadTool]) -> list[HostedTool]:
        """Инструменты с телом в процессе теста: без обёртки запуска."""
        tools = self._hosting.toolset(payloads)
        self._tools.extend(tools)

        return list(tools)

    def adopt(self, tools: Sequence[HostedTool]) -> None:
        """Готовые инструменты хоста рядом с собранными стендом."""
        self._tools.extend(tools)

    def tools(self) -> list[HostedTool]:
        """Собранные инструменты стенда в порядке сборки, со сливом dev_null."""
        return list(self._tools)

    def specs(self) -> StreamSpecs:
        """Потоковые декларации собранных инструментов."""
        return self._specs

    def access(self, role: str, profile: str) -> ToolAccess:
        """Права, при которых роль role и профиль profile видят все
        собранные инструменты стенда."""
        names: list[str] = []
        for tool in self._tools:
            names.append(tool.name)

        return ToolAccess(
            tool_names=names,
            roles={role: RoleConfig(tools=["*"])},
            profiles={profile: ProfileGrant(tools=["*"], roles=["*"])},
        )

    def registry(
        self, access: ToolAccess, node_args: Sequence[NodeArgs]
    ) -> ToolRegistry:
        """Реестр собранных инструментов под обвязками цепочки и правами
        access; node_args — правила аргументов узлов. Обвязки ложатся на
        инструменты один раз: реестр у стенда один."""
        tools = list(self._tools)
        self._chain.seal(tools, access, self._specs)

        return ToolRegistry(
            tools=tools,
            access=access,
            stream_config=self._stream,
            own=frozenset(),
            node_args=tuple(node_args),
            specs=self._specs,
            ambient=self._ambient,
        )

    def server(
        self, access: ToolAccess, node_args: Sequence[NodeArgs], calls: NodeCalls
    ) -> ToolServer:
        """Порт инструментов над реестром стенда; calls — путь вызова узла
        к телу."""
        registry = self.registry(access, node_args)

        return registry.server(registry.tools, calls)

    def _drained(self, tools: Sequence[HostedTool], specs: StreamSpecs) -> None:
        """Слив dev_null рядом с писателями каналов; ставится один раз."""
        if self._specs.of(DevNullTool.NAME).streaming():
            return

        for tool in tools:
            if not specs.of(tool.name).outbound():
                continue

            self._tools.append(self._hosting.hosted(self._drain.build()))
            self._specs = self._specs.declaring(DevNullTool.NAME, self._drain.spec())
            return
