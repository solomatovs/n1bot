"""Вызов инструмента вне хода чата: REST, планировщик.

Инструменты — уже собранные реестром с полной цепочкой хуков и отобранные
под субъекта (ToolRegistry.for_headless). ToolInvoker собирает вызов-узел
со служебным полем подписи и исполняет его тем же исполнителем, что и чат:
DAG из одного узла в DagRunner. Контекст и запуск открывает вызывающий:
Runs.open.

Ошибки:
ToolUnavailableError — инструмента нет среди видимых субъекту вне чата.
StreamPlanError, DagRunError — вызов не переводится в план.
pydantic.ValidationError — аргументы вызова не значения JSON.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from boba.toolkit.calls import CallIdPrefix, ToolIntent
from boba.toolkit.dag import DagNode, DagSpec, NodeOutcome
from boba.toolkit.failure import ToolUnavailableError
from boba.toolrun.dag_run import DagRunner
from boba.toolrun.hosted import HostedTool

__all__ = ["ToolInvoker"]


class ToolInvoker:
    """Вызовы инструментов, видимых субъекту вне чата.

    Создаётся входом приложения (REST) из инструментов субъекта
    (ToolRegistry.for_headless) и исполнителя DAG над ними
    (ToolRegistry.runner); исполняет вызов DagRunner'ом — другого места
    исполнения инструментов нет.
    """

    def __init__(self, tools: Mapping[str, HostedTool], runner: DagRunner) -> None:
        self._tools = dict(tools)
        self._runner = runner

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def tool(self, name: str) -> HostedTool:
        tool = self._tools.get(name)
        if tool is None:
            known = ", ".join(sorted(self._tools))
            if not known:
                known = "none"

            msg = (
                f"tool {name!r} is not available to the caller; "
                f"available tools: {known}"
            )
            raise ToolUnavailableError(msg)

        return tool

    def call(
        self, name: str, args: Mapping[str, Any], intent: str, prefix: CallIdPrefix
    ) -> DagNode:
        """Вызов инструмента name узлом с новым идентификатором источника prefix."""
        call_args: dict[str, Any] = dict(args)
        call_args[ToolIntent.NAME] = intent

        return DagNode(key=prefix.new_id(), tool=name, args=call_args)

    async def invoke(self, call: DagNode) -> NodeOutcome:
        """Исполнить вызов DAG'ом из одного узла и отдать итог узла."""
        self.tool(call.tool)

        dag = DagSpec(name=call.key, version=1, nodes=[call])
        outcome = await self._runner.run(dag)

        return outcome.nodes[0]
