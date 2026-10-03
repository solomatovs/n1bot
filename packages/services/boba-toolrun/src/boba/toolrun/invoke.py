"""Вызов инструмента вне хода чата: REST, планировщик.

Инструменты — уже собранные реестром с полной цепочкой хуков и отобранные
под субъекта (ToolRegistry.for_headless). ToolInvoker собирает ToolCall со
служебными полями (id, intent) и исполняет его тем же исполнителем, что и
чат: DAG из одного узла в DagRunner. Контекст и запуск открывает
вызывающий: Runs.open.

Ошибки:
ToolUnavailableError — инструмента нет среди видимых субъекту вне чата.
StreamPlanError, DagRunError — вызов не переводится в план либо инструмент
    ответил не сообщением с результатом.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.messages import ToolCall
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tools import BaseTool

from boba.identity.context import Subject
from boba.toolkit.calls import CallIdPrefix, ToolIntent
from boba.toolkit.dag import DagNode, DagSpec
from boba.toolkit.failure import ToolUnavailableError
from boba.toolrun.dag_run import DagRunner, NodeOutcome
from boba.toolrun.registry import ToolRegistry

__all__ = ["ToolInvoker"]


class ToolInvoker:
    """Вызовы инструментов, видимых субъекту вне чата.

    Создаётся из инструментов субъекта и исполнителя DAG над ними (его
    собирает реестр инструментов); исполняет вызов DagRunner'ом — другого
    места исполнения инструментов нет.
    """

    def __init__(self, tools: Mapping[str, BaseTool], runner: DagRunner) -> None:
        self._tools = dict(tools)
        self._runner = runner

    @classmethod
    def for_subject(cls, registry: ToolRegistry, subject: Subject) -> ToolInvoker:
        """Инструменты субъекта вне чата: по его ролям и профилю."""
        tools = registry.for_headless(subject.roles, subject.profile)

        return cls(tools, registry.runner(tools))

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def tool(self, name: str) -> BaseTool:
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

    @staticmethod
    def call(
        name: str, args: Mapping[str, Any], intent: str, prefix: CallIdPrefix
    ) -> ToolCall:
        call_args: dict[str, Any] = dict(args)
        call_args[ToolIntent.NAME] = intent

        return ToolCall(name=name, args=call_args, id=prefix.new_id(), type="tool_call")

    async def invoke(self, call: ToolCall) -> NodeOutcome:
        """Вызов вне дерева колбэков вызывающего: из хода чата фоновые задачи
        в ленту не попадают, их итог несёт отчёт самого запуска."""
        self.tool(call["name"])

        key = str(call["id"])
        node = DagNode(key=key, tool=call["name"], args=dict(call["args"]))
        dag = DagSpec(name=key, version=1, nodes=[node])

        detached = var_child_runnable_config.set(None)
        try:
            outcome = await self._runner.run(dag)
        finally:
            var_child_runnable_config.reset(detached)

        return outcome.nodes[0]
