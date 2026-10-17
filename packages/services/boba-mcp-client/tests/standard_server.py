"""Обычный MCP-сервер на официальном SDK: про boba он ничего не знает.

Запускается субпроцессом из тестов клиента: `python standard_server.py stdio`
либо `python standard_server.py streamable-http <port>`; транспорт
`stdio-extended` — тот же сервер, объявляющий расширения с настройками не
по моделям клиента (DeclaredExtension). Кроме четырёх
обычных инструментов держит отказы стенда: задержку, большой результат,
обрыв посреди вызова, смену списка инструментов с уведомлением и отчёты о
ходе вызова, — ответ из блоков разных видов и адресатов, файл, который
клиенту стоит показать сразу (priority 1), просьбу о вводе
(input_required) и отказ ошибкой MCP. Расширение MCP Apps даёт приложение
доски (board.html): инструмент board рисует разметку, которую пишет модель,
app_only зовёт только приложение, model_only — только модель.
"""

import asyncio
import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.extension import Extension
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    Annotations,
    CallToolResult,
    EmbeddedResource,
    InputRequiredResult,
    ResourceLink,
    TextContent,
    TextResourceContents,
    ToolAnnotations,
)


class DeclaredExtension(Extension):
    """Расширение, которое сервер объявляет с заданными настройками."""

    def __init__(self, identifier: str, settings: dict[str, Any]) -> None:
        self.identifier = identifier
        self._settings = settings

    def settings(self) -> dict[str, Any]:
        return dict(self._settings)


class StandardServer:
    """Сервер с инструментами: число, текст, сбой, картинка, отказы стенда и
    приложение доски."""

    PNG: bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    EXTRA: ClassVar[str] = "extra"
    LARGE_LINE: ClassVar[str] = "0123456789"
    ASKED: ClassVar[str] = "asked once"
    REFUSAL: ClassVar[dict[str, str]] = {"reason": "the stand refuses"}
    BOARD: ClassVar[str] = "ui://standard/board.html"
    BOARD_CSP: ClassVar[ResourceCsp] = ResourceCsp(
        resource_domains=["https://cdn.example.com"]
    )
    DOCUMENT: ClassVar[dict[str, Any]] = {
        "rows": [{"id": 1, "name": "<script>window.__boba_pwned=1</script>"}],
        "total": 1,
    }
    """JSON ответа blocks человеку: дерево в ленте, строка со скриптом — текстом."""
    EXTENSIONS: ClassVar[tuple[Extension, ...]] = (
        DeclaredExtension(
            "com.boba/journal",
            {
                "read": "add",
                "path": "/journal",
                "resource": "journal://{run}/{node}/{channel}",
                "since": 2,
            },
        ),
        DeclaredExtension("com.boba/nested-calls", {"tool": "workflow"}),
    )

    def __init__(self, port: int, extended: bool) -> None:
        apps = Apps()
        apps.tool(resource_uri=self.BOARD)(self.board)
        apps.tool(resource_uri=self.BOARD, visibility=["app"])(self.app_only)
        apps.tool(resource_uri=self.BOARD, visibility=["model"])(self.model_only)
        html = Path(__file__).with_name("board.html").read_text()
        apps.add_html_resource(self.BOARD, html, csp=self.BOARD_CSP)
        extensions: tuple[Extension, ...] = (apps,)
        if extended:
            extensions = (apps, *self.EXTENSIONS)

        self._mcp = MCPServer("standard", extensions=extensions)
        self._port = port
        self._extra_on = False
        self._mcp.tool(annotations=ToolAnnotations(read_only_hint=True))(self.add)
        self._mcp.tool(
            annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False)
        )(self.shout)
        self._mcp.tool()(self.broken)
        self._mcp.tool()(self.picture)
        self._mcp.tool()(self.blocks)
        self._mcp.tool()(self.show)
        self._mcp.tool()(self.sleep_for)
        self._mcp.tool()(self.large)
        self._mcp.tool()(self.die)
        self._mcp.tool()(self.toggle_extra)
        self._mcp.tool(annotations=ToolAnnotations(read_only_hint=True))(self.progress)
        self._mcp.tool()(self.ask)

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
                EmbeddedResource(
                    resource=TextResourceContents(
                        uri="file:///answer.json",
                        mime_type="application/json",
                        text=json.dumps(self.DOCUMENT),
                    ),
                    annotations=Annotations(audience=["user"]),
                ),
            ]
        )

    def show(self, path: str, mime: str) -> CallToolResult:
        """Answers with a workspace file the client should show right away."""
        link = ResourceLink(
            name=PurePosixPath(path).name,
            uri=PurePosixPath(path).as_uri(),
            mime_type=mime,
            annotations=Annotations(audience=["user"], priority=1.0),
        )

        return CallToolResult(content=[TextContent(text="shown"), link])

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

    def ask(self, refuse: bool) -> InputRequiredResult:
        """Asks the client for more input instead of answering, or refuses
        the request with an MCP error."""
        if refuse:
            msg = "ask refuses the request"
            raise MCPError(code=INVALID_PARAMS, message=msg, data=self.REFUSAL)

        return InputRequiredResult(request_state=self.ASKED)

    def board(self, ctx: Context, markup: str) -> str:
        """Draws the given HTML markup on a board."""
        if client_supports_apps(ctx):
            return "the board is drawn by its app"

        return f"board markup: {markup}"

    def app_only(self) -> str:
        """Refreshes the board; only its app calls it."""
        return "app_only answered"

    def model_only(self) -> str:
        """Clears the board; only the model calls it."""
        return "model_only answered"

    def run(self, transport: str) -> None:
        if transport.startswith("stdio"):
            self._mcp.run("stdio")
            return

        self._mcp.run("streamable-http", host="127.0.0.1", port=self._port)


if __name__ == "__main__":
    port = 0
    if len(sys.argv) > 2:
        port = int(sys.argv[2])

    StandardServer(port, sys.argv[1] == "stdio-extended").run(sys.argv[1])
