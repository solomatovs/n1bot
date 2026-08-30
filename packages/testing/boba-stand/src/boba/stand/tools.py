"""Инструменты стенда для тестов исполнения: зонд контекста, задержка, эхо, отказ."""

import asyncio
from typing import Annotated, Any

from langchain_core.tools import tool

from boba.access import ProfileGrant, RoleConfig, ToolAccess
from boba.identity.context import CallContext
from boba.runtime.plugins import CallSurface
from boba.stand.context import TEST_PROFILE
from boba.toolkit.result import ErrorResult, Produces, TextResult, pack_result
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

        @tool(response_format="content_and_artifact")
        async def slow(label: str, delay: float) -> tuple[str, Any]:
            """Спит delay секунд, отдаёт label."""
            contexts.append(CallContext.current())
            await asyncio.sleep(delay)
            return pack_result(TextResult(text=f"done {label}"))

        @tool(response_format="content_and_artifact")
        async def echo(
            text: str,
        ) -> Annotated[tuple[str, Any], Produces.of(TextResult)]:
            """Отдаёт text."""
            return pack_result(TextResult(text=text))

        @tool(response_format="content_and_artifact")
        async def fail(text: str) -> tuple[str, Any]:
            """Отказ результатом."""
            return pack_result(ErrorResult(message=text, error_kind="probe"))

        @tool(response_format="content_and_artifact")
        async def canvas_open(path: str) -> tuple[str, Any]:
            """Инструмент чата: в workflow не допускается."""
            return pack_result(TextResult(text=path))

        tools = [slow, echo, fail, canvas_open]
        ToolCallIdField.attach_all(tools)
        ToolIntentField.attach_all(tools)
        ToolRunLogger.guard_all(
            tools, CallSurface.stream_source, CallSurface.tool_call_scope
        )
        ToolErrorGuard.guard_all(tools)
        return tools

    def registry(self, granted: list[str], profile: str = TEST_PROFILE) -> ToolRegistry:
        """Реестр с этими инструментами: роль PROBE_ROLE видит всё, профиль — granted."""
        tools = self.tools()
        names: list[str] = []
        for tool_ in tools:
            names.append(tool_.name)

        access = ToolAccess(
            tool_names=names,
            roles={PROBE_ROLE: RoleConfig(tools=["*"])},
            profiles={profile: ProfileGrant(tools=granted, roles=["*"])},
            chat_only=["canvas_open"],
        )
        return ToolRegistry(tools=tools, access=access)
