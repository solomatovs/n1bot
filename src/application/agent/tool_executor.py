"""Выполнение tool calls через ToolRegistry."""
from __future__ import annotations

from typing import Iterator, List

from application.agent.llm_client import ToolCallData
from domain.agent.context_window import ContextWindow
from domain.agent.events import DocPipelineEvent, ToolCallStarted, ToolResultReady
from domain.agent.tools import ToolEvent, ToolExecutionError, ToolNotFoundError, ToolRegistry, ToolResult


class ToolCallExecutor:
    """Выполняет tool calls через ToolRegistry, обновляет ContextWindow."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def execute(
        self,
        tool_calls: List[ToolCallData],
        window: ContextWindow,
    ) -> Iterator[DocPipelineEvent]:
        """Выполнить tool calls, yield'ить события, обновить window."""
        window.add_tool_calls([tc.to_dict() for tc in tool_calls])

        for tc in tool_calls:
            yield ToolCallStarted(
                tool_call_id=tc.id,
                tool_name=tc.name,
                arguments=tc.arguments,
            )

            result_text = ""
            try:
                for output in self._registry.execute(tc.name, tc.parse_arguments()):
                    match output:
                        case ToolEvent(event=e):
                            yield e
                        case ToolResult(content=c):
                            result_text = c
            except (ToolNotFoundError, ToolExecutionError) as exc:
                result_text = str(exc)

            yield ToolResultReady(
                tool_call_id=tc.id,
                tool_name=tc.name,
                content=result_text[:2000],
            )

            window.add_tool_result(tc.id, result_text)
