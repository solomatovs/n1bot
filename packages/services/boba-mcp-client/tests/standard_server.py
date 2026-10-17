"""Обычный MCP-сервер на официальном SDK: про boba он ничего не знает.

Запускается субпроцессом из тестов клиента: `python standard_server.py stdio`
либо `python standard_server.py streamable-http <port>`; транспорт
`stdio-extended` — тот же сервер, объявляющий расширения с настройками не
по моделям клиента (DeclaredExtension). На нём есть каждый запрос и каждое
уведомление редакции 2026-07-28, кроме устаревших (sampling, roots,
logging), чтобы клиент проверялся на всём: server/discover с инструкциями;
инструменты — обычные, отказы стенда (задержка, большой результат, обрыв
посреди вызова), ход вызова, ответ из блоков разных видов и адресатов,
файл, который клиенту стоит показать сразу (priority 1), просьба о вводе
(input_required) — пустая и с формой вопроса пользователю (load_rows) — и
отказ ошибкой MCP; ресурсы — текст, двоичный, шаблон с
подсказками значений и шаблон, просящий ввода; промпты — с аргументами,
сообщениями обеих ролей и просящий ввода; completion/complete для
аргументов промпта и шаблона; потоки subscriptions/listen со сменой списков
инструментов (toggle_extra), ресурсов (grow_resources), промптов
(toggle_prompt) и ростом ресурса (bump). Расширение MCP Apps даёт
приложение доски (board.html): инструмент board рисует разметку, которую
пишет модель, app_only зовёт только приложение, model_only — только модель.
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
from mcp.server.mcpserver.prompts.base import AssistantMessage, Message, UserMessage
from mcp.server.mcpserver.resources.types import TextResource
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INVALID_PARAMS,
    Annotations,
    CallToolResult,
    Completion,
    CompletionArgument,
    CompletionContext,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    EmbeddedResource,
    InputRequiredResult,
    PromptReference,
    ResourceLink,
    ResourceTemplateReference,
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
    EXTRA_PROMPT: ClassVar[str] = "extra_prompt"
    INSTRUCTIONS: ClassVar[str] = "Call add for sums; read standard://notes first."
    NOTES: ClassVar[str] = "standard://notes"
    NOTES_TEXT: ClassVar[str] = "the notes of the standard server"
    LOGO: ClassVar[str] = "standard://logo.png"
    COUNTER: ClassVar[str] = "standard://counter"
    ITEMS: ClassVar[tuple[str, ...]] = ("apple", "apricot", "banana")
    STYLES: ClassVar[tuple[str, ...]] = ("plain", "poem", "pirate")
    LARGE_LINE: ClassVar[str] = "0123456789"
    ASKED: ClassVar[str] = "asked once"
    TARGET: ClassVar[str] = "target"
    TARGET_STATE: ClassVar[str] = "load_rows asked for the target"
    TARGET_MESSAGE: ClassVar[str] = (
        "load_rows needs the target table before it can start the copy."
    )
    TARGET_FORM: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "table": {
                "type": "string",
                "title": "Target",
                "description": "Which table should receive the rows?",
                "oneOf": [
                    {"const": "sales.orders", "title": "sales.orders"},
                    {"const": "sales.orders_stage", "title": "sales.orders_stage"},
                    {"const": "tmp.orders_copy", "title": "tmp.orders_copy"},
                ],
            }
        },
        "required": ["table"],
    }
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

        self._mcp = MCPServer(
            "standard",
            instructions=self.INSTRUCTIONS,
            version="1.0",
            extensions=extensions,
        )
        self._port = port
        self._extra_on = False
        self._extra_prompt_on = False
        self._grown = 0
        self._count = 0
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
        self._mcp.tool()(self.load_rows)
        self._mcp.tool()(self.client)
        self._mcp.tool()(self.toggle_prompt)
        self._mcp.tool()(self.grow_resources)
        self._mcp.tool()(self.bump)
        self._mcp.resource(self.NOTES, mime_type="text/plain")(self.notes)
        self._mcp.resource(self.LOGO, mime_type="image/png")(self.logo)
        self._mcp.resource(self.COUNTER, mime_type="text/plain")(self.counter)
        self._mcp.resource("standard://items/{item}", mime_type="text/plain")(self.item)
        self._mcp.resource("standard://ask/{topic}", mime_type="text/plain")(
            self.ask_resource
        )
        self._mcp.prompt()(self.greet)
        self._mcp.prompt()(self.ask_prompt)
        self._mcp.completion()(self.complete)

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

    def load_rows(self, ctx: Context, rows: int) -> str | InputRequiredResult:
        """Loads the rows into a table the user chooses."""
        responses = ctx.input_responses
        if not responses:
            form = ElicitRequestFormParams(
                message=self.TARGET_MESSAGE,
                requested_schema=self.TARGET_FORM,
            )
            return InputRequiredResult(
                input_requests={self.TARGET: ElicitRequest(params=form)},
                request_state=self.TARGET_STATE,
            )

        if ctx.request_state != self.TARGET_STATE:
            msg = f"load_rows got request state {ctx.request_state!r}"
            raise ToolError(msg)

        answer = responses.get(self.TARGET)
        if not isinstance(answer, ElicitResult):
            msg = f"load_rows expected an elicitation answer, got {answer!r}"
            raise ToolError(msg)

        if answer.action != "accept" or answer.content is None:
            msg = f"the user answered {answer.action}"
            raise ToolError(msg)

        return f"loaded {rows} rows into {answer.content['table']}"

    def client(self, ctx: Context) -> str:
        """Returns the clientInfo and clientCapabilities of the request as JSON."""
        info: dict[str, Any] | None = None
        params = ctx.session.client_params
        if params is not None:
            info = params.client_info.model_dump(mode="json", exclude_none=True)

        capabilities: dict[str, Any] | None = None
        declared = ctx.client_capabilities
        if declared is not None:
            capabilities = declared.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )

        return json.dumps({"info": info, "capabilities": capabilities})

    async def toggle_prompt(self, ctx: Context) -> str:
        """Adds or removes the prompt `extra_prompt` and notifies the client."""
        if self._extra_prompt_on:
            self._mcp.remove_prompt(self.EXTRA_PROMPT)
            self._extra_prompt_on = False
        else:
            self._mcp.prompt(name=self.EXTRA_PROMPT)(self.extra_prompt)
            self._extra_prompt_on = True

        await ctx.notify_prompts_changed()
        return f"extra prompt on: {self._extra_prompt_on}"

    def extra_prompt(self) -> str:
        """Appears only after toggle_prompt."""
        return "the extra prompt"

    async def grow_resources(self, ctx: Context) -> str:
        """Adds one more text resource and notifies the client."""
        self._grown += 1
        uri = f"standard://grown/{self._grown}"
        self._mcp.add_resource(
            TextResource(uri=uri, name=f"grown {self._grown}", text=uri)
        )
        await ctx.notify_resources_changed()
        return uri

    async def bump(self, ctx: Context) -> str:
        """Bumps the counter resource and notifies its subscribers."""
        self._count += 1
        await ctx.notify_resource_updated(self.COUNTER)
        return f"counter {self._count}"

    def notes(self) -> str:
        """Notes of the standard server."""
        return self.NOTES_TEXT

    def logo(self) -> bytes:
        """A tiny PNG logo."""
        return self.PNG

    def counter(self) -> str:
        """The value bump raises."""
        return str(self._count)

    def item(self, item: str) -> str:
        """An item of the shop by name."""
        return f"item {item}"

    def ask_resource(self, topic: str) -> InputRequiredResult:
        """Asks the client for more input instead of the resource."""
        return InputRequiredResult(request_state=f"{self.ASKED} {topic}")

    def greet(self, name: str, style: str = "plain") -> list[Message]:
        """Greets a person in the given style."""
        return [
            UserMessage(f"Greet {name} in the {style} style."),
            AssistantMessage(f"Hello, {name}!"),
        ]

    def ask_prompt(self) -> InputRequiredResult:
        """Asks the client for more input instead of the messages."""
        return InputRequiredResult(request_state=self.ASKED)

    async def complete(
        self,
        ref: PromptReference | ResourceTemplateReference,
        argument: CompletionArgument,
        context: CompletionContext | None,
    ) -> Completion | None:
        """Values of the style of greet and the item of the item template
        that start with what is typed."""
        wanted = (ref.type, argument.name)
        known: tuple[str, ...] = ()
        if wanted == ("ref/prompt", "style"):
            known = self.STYLES

        if wanted == ("ref/resource", "item"):
            known = self.ITEMS

        values: list[str] = []
        for value in known:
            if value.startswith(argument.value):
                values.append(value)

        return Completion(values=values, total=len(values), has_more=False)

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
