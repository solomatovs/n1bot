"""Инструменты стенда для тестов исполнения: зонд контекста, задержка, эхо, отказ."""

import asyncio
from typing import Any

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.identity.context import CallContext
from boba.runtime.launchers import CallSurface
from boba.runtime.plugins import ToolBridge
from boba.stand.context import TEST_PROFILE
from boba.toolkit.facade import tool
from boba.toolkit.result import ErrorResult, MarkdownResult
from boba.toolrun.call_id import ToolCallIdField
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.intent import ToolIntentField
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.run_log import ToolRunLogger

PROBE_ROLE = "wf"
"""Роль, которой стенд выдаёт все инструменты зонда."""


class Probe:
    """Инструменты стенда: задержка, эхо, отказ, зонд контекста."""

    def __init__(self) -> None:
        self.contexts: list[CallContext] = []

    def tools(self) -> list[Any]:
        contexts = self.contexts

        @tool
        async def slow(label: str, delay: float) -> MarkdownResult:
            """Спит delay секунд, отдаёт label."""
            contexts.append(CallContext.current())
            await asyncio.sleep(delay)
            return MarkdownResult(text=f"done {label}")

        @tool
        async def echo(text: str) -> MarkdownResult:
            """Отдаёт text."""
            return MarkdownResult(text=text)

        @tool
        async def fail(text: str) -> ErrorResult:
            """Отказ результатом."""
            return ErrorResult(message=text, error_kind="probe")

        tools = list(ToolBridge.toolset([slow, echo, fail]))
        ToolCallIdField.attach_all(tools)
        ToolIntentField.attach_all(tools)
        ToolRunLogger.guard_all(
            tools, CallSurface.stream_source, CallSurface.tool_call_scope
        )
        ToolErrorGuard.guard_all(tools)
        return tools

    def registry(self, granted: list[str], profile: str = TEST_PROFILE) -> ToolRegistry:
        """Реестр с инструментами: роль PROBE_ROLE видит всё, профиль — granted."""
        tools = self.tools()
        names: list[str] = []
        for tool_ in tools:
            names.append(tool_.name)

        access = ToolAccess(
            tool_names=names,
            roles={PROBE_ROLE: RoleConfig(tools=["*"])},
            profiles={profile: ProfileGrant(tools=granted, roles=["*"])},
        )
        return ToolRegistry(tools=tools, access=access)
