"""Конвейеры инструментов из чата: pipeline_catalog и pipeline_run.

LLM не видит потоковые инструменты-насосы как функции для прямого вызова —
их данные предназначены не модели, а другому инструменту. Вместо этого
модель получает два инструмента оркестрации: pipeline_catalog показывает
каталог узлов (потоковые инструменты с их kind'ами входа и выхода), а
pipeline_run принимает линейную цепочку узлов, проверяет стыковку по
декларациям (ChainCheck) и запускает её.

Узлы исполняются через обычный реестр — с полной цепочкой хуков (доступ,
журнал, injected-конфиги, соединения пользователя). PipelineSlot переводит
обёртку запуска каждого узла в потоковый режим: каналы рёбер отдаются
дескрипторами и соединяются splice'ом — данные текут между процессами
через ядро, в контекст модели попадают только конверты узлов.

Ошибки:
ErrorResult — план не разобран, узел неизвестен, декларации не стыкуются,
    узел не отдал канал; текст пригоден модели для исправления плана.
Прочие исключения узлов упаковывают хуки реестра, исключения оркестратора —
    ToolErrorGuard вызывающего инструмента.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, ClassVar

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.identity.context import CallContext
from boba.toolkit.calls import CallIdPrefix, ScriptCall, ToolCallViews
from boba.toolkit.chain import (
    CallRelay,
    ChainCheck,
    ChainMismatchError,
    NodeSlot,
    PipelineSlot,
    RelayStats,
)
from boba.toolkit.ports import PortDirection, StreamSpec, ToolStreamSpecs
from boba.toolkit.result import ErrorResult, TextResult, ToolResult, pack_result
from boba.toolrun.invoke import InvokeReply, ToolInvoker
from boba.toolrun.registry import ToolRegistry

__all__ = [
    "PipelinePlan",
    "PipelineService",
    "PipelineToolConfig",
    "build_pipeline_tools",
]

RegistrySource = Callable[[], Awaitable[ToolRegistry]]


class PipelineToolConfig(BaseModel):
    """Секция [tool.pipeline]: своих параметров у инструментов нет."""

    model_config = ConfigDict(extra="ignore")


class PipelineNodeSpec(BaseModel):
    """Один узел плана: имя инструмента и его аргументы."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class PipelinePlan(BaseModel):
    """Линейная цепочка узлов: выход каждого идёт во вход следующего."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[PipelineNodeSpec, ...] = Field(min_length=2)


class PipelineErrorKind:
    """Kind'ы отказов оркестратора конвейера для ErrorResult."""

    PLAN: ClassVar[str] = "pipeline_plan_invalid"
    CHAIN: ClassVar[str] = "pipeline_chain_mismatch"
    RUN: ClassVar[str] = "pipeline_failed"


class PipelinePrompt:
    """Описания инструментов конвейера для модели."""

    CATALOG: ClassVar[str] = (
        "List streaming tools available as pipeline nodes: their input and "
        "output frame kinds and arguments. Use it before pipeline_run to "
        "pick compatible nodes. Streaming tools move data directly between "
        "each other and are not callable one by one."
    )

    RUN: ClassVar[str] = (
        "Run a linear pipeline of streaming tools: the output of each node "
        "feeds the input of the next, data flows between processes without "
        "entering the chat context. Pass a JSON plan: "
        '{"nodes": [{"tool": "<name>", "args": {...}}, ...]}. '
        "Node names and arguments come from pipeline_catalog; adjacent "
        "nodes must have compatible kinds (checked before start). Returns "
        "per-node results and transferred byte counts."
    )

    PLAN: ClassVar[str] = (
        'Pipeline plan as JSON: {"nodes": [{"tool": "<name>", '
        '"args": {"<arg>": <value>}}, ...]}; at least two nodes, listed '
        "in flow order."
    )


class PipelineService:
    """Каталог узлов и запуск линейных конвейеров поверх реестра."""

    FD_WAIT_SEC: ClassVar[float] = 60.0
    """Сколько ждать дескриптор канала узла: узел, упавший до открытия
    вызова (доступ, битые аргументы), канала не отдаст."""

    def catalog(self, invoker: ToolInvoker) -> ToolResult:
        """Каталог узлов: потоковые инструменты, видимые субъекту."""
        lines: list[str] = []

        for name in sorted(invoker.names):
            spec = ToolStreamSpecs.of(name)
            if not spec.streaming():
                continue

            lines.append(self._node_line(name, spec, invoker))

        if not lines:
            return TextResult(text="no streaming tools are available")

        header = "streaming tools (pipeline nodes):"
        return TextResult(text="\n".join([header, *lines]))

    async def run(self, invoker: ToolInvoker, plan_text: str) -> ToolResult:
        """Разобрать план, проверить стыковку и прогнать цепочку."""
        try:
            plan = PipelinePlan.model_validate_json(plan_text)
        except ValidationError as exc:
            return ErrorResult(
                message=f"pipeline plan is invalid: {exc}",
                error_kind=PipelineErrorKind.PLAN,
            )

        try:
            specs = self._checked_specs(invoker, plan)
        except ChainMismatchError as exc:
            return ErrorResult(message=str(exc), error_kind=PipelineErrorKind.CHAIN)

        return await self._execute(invoker, plan, specs)

    def _checked_specs(
        self, invoker: ToolInvoker, plan: PipelinePlan
    ) -> tuple[StreamSpec, ...]:
        """Спеки узлов плана; несуществующий узел или нестыковка — отказ."""
        specs: list[StreamSpec] = []
        for node in plan.nodes:
            if node.tool not in invoker.names:
                msg = (
                    f"unknown pipeline node {node.tool!r}: pick nodes from "
                    "pipeline_catalog"
                )
                raise ChainMismatchError(msg)

            spec = ToolStreamSpecs.of(node.tool)
            if not spec.streaming():
                msg = (
                    f"tool {node.tool!r} declares no stream ports and cannot "
                    "be a pipeline node"
                )
                raise ChainMismatchError(msg)

            specs.append(spec)

        for left, right in zip(specs, specs[1:], strict=False):
            ChainCheck.ensure(left, right)

        return tuple(specs)

    async def _execute(
        self,
        invoker: ToolInvoker,
        plan: PipelinePlan,
        specs: Sequence[StreamSpec],
    ) -> ToolResult:
        last = len(plan.nodes) - 1

        slots: list[NodeSlot] = []
        for index in range(len(plan.nodes)):
            slots.append(
                NodeSlot(has_upstream=index > 0, has_downstream=index < last)
            )

        node_tasks = self._start_nodes(invoker, plan, slots)

        try:
            relay_stats = await self._wire_edges(slots)
        except ChainMismatchError as exc:
            for slot in slots:
                slot.abort()

            await asyncio.gather(*node_tasks, return_exceptions=True)
            return ErrorResult(message=str(exc), error_kind=PipelineErrorKind.CHAIN)

        replies = await asyncio.gather(*node_tasks, return_exceptions=True)

        return await self._report(plan, replies, relay_stats)

    def _start_nodes(
        self,
        invoker: ToolInvoker,
        plan: PipelinePlan,
        slots: Sequence[NodeSlot],
    ) -> list[asyncio.Task[InvokeReply]]:
        """Задачи узлов: каждая уносит свой слот в копии контекста."""
        tasks: list[asyncio.Task[InvokeReply]] = []

        for node, slot in zip(plan.nodes, slots, strict=True):
            call = ToolInvoker.call(
                node.tool, node.args, f"pipeline: {node.tool}", CallIdPrefix.PIPELINE
            )

            token = PipelineSlot.set(slot)
            try:
                tasks.append(
                    asyncio.create_task(
                        invoker.invoke(call), name=f"pipeline-node:{node.tool}"
                    )
                )
            finally:
                PipelineSlot.reset(token)

        return tasks

    async def _wire_edges(
        self, slots: Sequence[NodeSlot]
    ) -> list[asyncio.Task[RelayStats]]:
        """Соединить рёбра: дескрипторы соседних узлов в splice-задачи."""
        relays: list[asyncio.Task[RelayStats]] = []

        for index in range(len(slots) - 1):
            source_fd = await asyncio.to_thread(
                slots[index].take_source_fd, self.FD_WAIT_SEC
            )
            sink_fd = await asyncio.to_thread(
                slots[index + 1].take_input_fd, self.FD_WAIT_SEC
            )

            relays.append(
                asyncio.create_task(
                    asyncio.to_thread(CallRelay.splice, source_fd, sink_fd),
                    name=f"pipeline-edge:{index}",
                )
            )

        return relays

    async def _report(
        self,
        plan: PipelinePlan,
        replies: Sequence[InvokeReply | BaseException],
        relays: Sequence[asyncio.Task[RelayStats]],
    ) -> ToolResult:
        stats = await asyncio.gather(*relays, return_exceptions=True)

        lines: list[str] = []
        failed = False
        for node, reply in zip(plan.nodes, replies, strict=True):
            if isinstance(reply, BaseException):
                failed = True
                lines.append(f"{node.tool}: crashed: {reply}")
                continue

            if not reply.ok:
                failed = True
                lines.append(f"{node.tool}: failed: {reply.error_text}")
                continue

            lines.append(f"{node.tool}: {reply.content}")

        for index, item in enumerate(stats):
            if isinstance(item, BaseException):
                failed = True
                lines.append(f"edge {index}: relay failed: {item}")
                continue

            lines.append(f"edge {index}: {item.bytes} bytes moved")

        text = "\n".join(lines)
        if failed:
            return ErrorResult(
                message=f"pipeline finished with errors:\n{text}",
                error_kind=PipelineErrorKind.RUN,
            )

        return TextResult(text=f"pipeline finished:\n{text}")

    def _node_line(
        self, name: str, spec: StreamSpec, invoker: ToolInvoker
    ) -> str:
        described = invoker.tool(name).description.strip().split("\n")[0]

        inbound = self._side(spec, PortDirection.INBOUND)
        outbound = self._side(spec, PortDirection.OUTBOUND)
        args = ", ".join(self._llm_args(invoker.tool(name)))

        return (
            f"- {name}: in={inbound} out={outbound}; "
            f"args: {args or 'none'}; {described}"
        )

    @staticmethod
    def _side(spec: StreamSpec, direction: PortDirection) -> str:
        if spec.raw(direction):
            return "raw"

        kinds = spec.kinds(direction)
        if not kinds:
            return "-"

        return "|".join(kinds)

    @staticmethod
    def _llm_args(node: BaseTool) -> list[str]:
        schema = node.tool_call_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            return []

        names: list[str] = []
        for field_name in schema.model_fields:
            names.append(field_name)

        return names


def build_pipeline_tools(
    cfg: PipelineToolConfig, registry: RegistrySource
) -> list[BaseTool]:
    """Инструменты pipeline_catalog / pipeline_run для реестра приложения."""
    service = PipelineService()

    ToolCallViews.register("pipeline_run", ScriptCall(arg="plan", lang="json"))

    async def _invoker() -> ToolInvoker:
        subject = CallContext.current().subject
        resolved = await registry()

        return ToolInvoker(resolved.for_headless(subject.roles, subject.profile))

    @tool(response_format="content_and_artifact")
    async def pipeline_catalog() -> tuple[str, ToolResult]:
        """Каталог узлов конвейера: потоковые инструменты и их kind'ы."""
        return pack_result(service.catalog(await _invoker()))

    @tool(response_format="content_and_artifact")
    async def pipeline_run(
        plan: Annotated[str, Field(min_length=1, description=PipelinePrompt.PLAN)],
    ) -> tuple[str, ToolResult]:
        """Запустить линейный конвейер потоковых инструментов."""
        return pack_result(await service.run(await _invoker(), plan))

    pipeline_catalog.description = PipelinePrompt.CATALOG
    pipeline_run.description = PipelinePrompt.RUN

    return [pipeline_catalog, pipeline_run]
