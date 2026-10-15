"""Обычный MCP-сервер на официальном SDK: про boba он ничего не знает.

Запускается субпроцессом из тестов клиента: `python standard_server.py stdio`
либо `python standard_server.py streamable-http <port>`. Кроме четырёх
обычных инструментов держит отказы стенда: задержку, большой результат,
обрыв посреди вызова, смену списка инструментов с уведомлением и отчёты о
ходе вызова, — и ответ из блоков разных видов и адресатов.
"""

import asyncio
import os
import sys
from typing import ClassVar

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import (
    Annotations,
    CallToolResult,
    EmbeddedResource,
    ResourceLink,
    TextContent,
    TextResourceContents,
    ToolAnnotations,
)


class StandardServer:
    """Сервер с инструментами: число, текст, сбой, картинка и отказы стенда."""

    PNG: bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    EXTRA: ClassVar[str] = "extra"
    LARGE_LINE: ClassVar[str] = "0123456789"

    def __init__(self, port: int) -> None:
        self._mcp = MCPServer("standard")
        self._port = port
        self._extra_on = False
        self._mcp.tool(annotations=ToolAnnotations(read_only_hint=True))(self.add)
        self._mcp.tool(
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False)
        )(self.shout)
        self._mcp.tool()(self.broken)
        self._mcp.tool()(self.picture)
        self._mcp.tool()(self.blocks)
        self._mcp.tool()(self.sleep_for)
        self._mcp.tool()(self.large)
        self._mcp.tool()(self.die)
        self._mcp.tool()(self.toggle_extra)
        self._mcp.tool(annotations=ToolAnnotations(read_only_hint=True))(self.progress)

    def add(self, a: int, b: int) -> int:
        """Adds two numbers."""
        return a + b

    def shout(self, text: str) -> str:
        """Returns the text in upper case."""
        return text.upper()

    def broken(self) -> str:
        """Always fails."""
        msg = "no such table: orders"
        raise ToolError(msg)

    def picture(self) -> Image:
        """Returns a tiny image."""
        return Image(data=self.PNG, format="png")

    def blocks(self, empty: bool) -> CallToolResult:
        """Answers with blocks for the user and the model, or only with
        structured content."""
        if empty:
            return CallToolResult(content=[], structured_content={"rows": 2})

        return CallToolResult(
            content=[
                TextContent(
                    text="for the user", annotations=Annotations(audience=["user"])
                ),
                TextContent(
                    text="for the model",
                    annotations=Annotations(audience=["user", "assistant"]),
                ),
                ResourceLink(
                    name="report.csv", uri="file:///report.csv", mime_type="text/csv"
                ),
                EmbeddedResource(
                    resource=TextResourceContents(
                        uri="file:///notes.txt", mime_type="text/plain", text="notes"
                    )
                ),
            ]
        )

    async def sleep_for(self, seconds: float) -> str:
        """Answers after the given delay."""
        await asyncio.sleep(seconds)
        return f"slept {seconds}"

    def large(self, size: int) -> str:
        """Returns a text of exactly the given size."""
        repeats = size // len(self.LARGE_LINE) + 1
        return (self.LARGE_LINE * repeats)[:size]

    def die(self) -> str:
        """Kills the server in the middle of the call."""
        os._exit(1)

    async def toggle_extra(self, ctx: Context) -> str:
        """Adds or removes the tool `extra` and notifies the client."""
        if self._extra_on:
            self._mcp.remove_tool(self.EXTRA)
            self._extra_on = False
        else:
            self._mcp.add_tool(self.extra, name=self.EXTRA)
            self._extra_on = True

        await ctx.notify_tools_changed()
        return f"extra on: {self._extra_on}"

    def extra(self) -> str:
        """Appears only after toggle_extra."""
        return "extra answered"

    async def progress(self, ctx: Context, steps: int) -> str:
        """Reports progress once per step, then answers."""
        for step in range(1, steps + 1):
            await ctx.report_progress(step, steps, f"step {step} of {steps}")
            await asyncio.sleep(0.01)

        return f"did {steps} steps"

    def run(self, transport: str) -> None:
        if transport == "stdio":
            self._mcp.run("stdio")
            return

        self._mcp.run("streamable-http", host="127.0.0.1", port=self._port)


if __name__ == "__main__":
    port = 0
    if len(sys.argv) > 2:
        port = int(sys.argv[2])

    StandardServer(port).run(sys.argv[1])
