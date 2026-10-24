"""Обычный MCP-сервер на официальном SDK: про boba он ничего не знает.

Запускается субпроцессом из тестов клиента: `python standard_server.py stdio`
либо `python standard_server.py streamable-http <port>`; транспорт
`stdio-extended` — тот же сервер, объявляющий расширения с настройками не
по моделям клиента (DeclaredExtension). `--ttl-ms N` — подсказки кэша
описания (public) и списка инструментов (private) со сроком N; `--retired
version|session` — сервер по HTTP с первого запроса не говорит на
2026-07-28 (ModernSwitch). Для проверок порта без состояния: pid — процесс
сервера, region — параметр с x-mcp-header, retitle — смена инструкций,
retire_modern — отказ от 2026-07-28 посреди жизни; `--workspace DIR` —
расширение файлов workspace с каталогом DIR (пути файлов show). На нём есть
каждый запрос и каждое
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
приложение заказов (orders.html): инструмент orders показывает строки
заказов, которые передаёт модель, refresh_orders зовёт только приложение,
archive_orders — только модель, orders_console открывает то же приложение
по статусу. Промпт orders_console просит модель вызвать этот инструмент и
несёт пометку расширения команд io.boba/commands, ведущую в его
приложение; `--commands` — сервер объявляет само расширение, без флага
пометку клиент обязан не замечать.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, ClassVar

import uvicorn
from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.caching import CacheHint
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
    Prompt,
    PromptReference,
    ResourceLink,
    ResourceTemplateReference,
    TextContent,
    TextResourceContents,
    ToolAnnotations,
)
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.types import Message as AsgiMessage


class MarkedPrompts(MCPServer):
    """MCPServer, чей prompts/list несёт _meta промптов из marks: высокий
    уровень SDK пометок промптов не задаёт."""

    def __init__(self, marks: dict[str, dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._marks = marks

    async def list_prompts(self) -> list[Prompt]:
        listed: list[Prompt] = []
        for prompt in await super().list_prompts():
            mark = self._marks.get(prompt.name)
            if mark is None:
                listed.append(prompt)
                continue

            listed.append(prompt.model_copy(update={"meta": mark}))

        return listed


class DeclaredExtension(Extension):
    """Расширение, которое сервер объявляет с заданными настройками."""

    def __init__(self, identifier: str, settings: dict[str, Any]) -> None:
        self.identifier = identifier
        self._settings = settings

    def settings(self) -> dict[str, Any]:
        return dict(self._settings)


class ModernSwitch:
    """ASGI-обёртка приложения сервера по HTTP. После retire_modern (или с
    флагом --retired на старте) сервер перестаёт говорить на 2026-07-28 и
    отвечает на запросы этой редакции отказом 400 — ошибкой версии -32022 с
    одной 2025-11-25 в supported (mode "version") либо ошибкой сессии
    сервера прежней редакции -32000 (mode "session"); рукопожатие initialize
    и запросы его сессии идут серверу как прежде. Вызов инструмента lost в
    сессии получает 404, как запрос сессии, которую сервер забыл, хотя
    сессия жива: так проверяется, что отказ одного запроса не достаётся
    соседнему."""

    MODERN: ClassVar[bytes] = b"2026-07-28"
    VERSION_HEADER: ClassVar[bytes] = b"mcp-protocol-version"
    SESSION_HEADER: ClassVar[bytes] = b"mcp-session-id"
    LOST: ClassVar[str] = "lost"

    def __init__(self, app: ASGIApp, mode: str) -> None:
        self._app = app
        self.mode = mode

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self._app(scope, receive, send)
            return

        body = b""
        more = True
        while more:
            message: AsgiMessage = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)

        headers = dict(scope["headers"])
        request = json.loads(body)
        if self._lost(headers, request):
            await self._answer(send, 404, {})
            return

        modern = headers.get(self.VERSION_HEADER) == self.MODERN
        if not self.mode or not modern:
            await self._app(scope, self._replayed(body, receive), send)
            return

        error: dict[str, Any] = {
            "code": -32000,
            "message": "Bad Request: No valid session ID provided",
        }
        if self.mode == "version":
            error = {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {"supported": ["2025-11-25"], "requested": "2026-07-28"},
            }

        payload = {"jsonrpc": "2.0", "id": request.get("id"), "error": error}
        await self._answer(send, 400, payload)

    def _lost(self, headers: dict[bytes, bytes], request: dict[str, Any]) -> bool:
        if self.SESSION_HEADER not in headers:
            return False

        if request.get("method") != "tools/call":
            return False

        return request.get("params", {}).get("name") == self.LOST

    def _replayed(self, body: bytes, later: Receive) -> Receive:
        """Тело, прочитанное обёрткой, — приложению первым сообщением; дальше
        — сообщения соединения как есть (конец соединения)."""
        pending = [body]

        async def receive() -> AsgiMessage:
            if not pending:
                return await later()

            return {"type": "http.request", "body": pending.pop(), "more_body": False}

        return receive

    async def _answer(self, send: Send, status: int, payload: dict[str, Any]) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(payload).encode()})


class OrderRow(BaseModel):
    """Строка заказа, которую модель передаёт приложению заказов."""

    id: int
    customer: str
    status: str
    total: float


class StandardServer:
    """Сервер с инструментами: число, текст, сбой, картинка, отказы стенда и
    приложение заказов."""

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
    ORDERS: ClassVar[str] = "ui://standard/orders.html"
    CONSOLE: ClassVar[str] = "orders_console"
    COMMANDS: ClassVar[str] = "io.boba/commands"
    COMMANDS_SETTINGS: ClassVar[dict[str, Any]] = {"version": "2026-10-24"}
    CONSOLE_MARK: ClassVar[dict[str, Any]] = {
        COMMANDS: {"action": "app", "tool": CONSOLE}
    }
    ORDERS_CSP: ClassVar[ResourceCsp] = ResourceCsp(
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

    def __init__(  # noqa: PLR0913 — сервер собирается всеми флагами запуска
        self,
        port: int,
        extended: bool,
        ttl_ms: int,
        retired: str,
        workspace: str,
        commands: bool,
    ) -> None:
        extensions = self._extensions(self._apps(), extended, workspace, commands)

        hints: dict[Any, CacheHint] = {}
        if ttl_ms:
            hints["server/discover"] = CacheHint(ttl_ms=ttl_ms, scope="public")
            hints["tools/list"] = CacheHint(ttl_ms=ttl_ms, scope="private")

        self._mcp = MarkedPrompts(
            {self.CONSOLE: self.CONSOLE_MARK},
            name="standard",
            instructions=self.INSTRUCTIONS,
            version="1.0",
            extensions=extensions,
            cache_hints=hints,
        )
        self._port = port
        self._retired = retired
        self._switch: ModernSwitch | None = None
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
        self._mcp.tool()(self.pid)
        self._mcp.tool()(self.region)
        self._mcp.tool()(self.area)
        self._mcp.tool()(self.retitle)
        self._mcp.tool()(self.retire_modern)
        self._mcp.resource(self.NOTES, mime_type="text/plain")(self.notes)
        self._mcp.resource(self.LOGO, mime_type="image/png")(self.logo)
        self._mcp.resource(self.COUNTER, mime_type="text/plain")(self.counter)
        self._mcp.resource("standard://items/{item}", mime_type="text/plain")(self.item)
        self._mcp.resource("standard://ask/{topic}", mime_type="text/plain")(
            self.ask_resource
        )
        self._mcp.prompt()(self.greet)
        self._mcp.prompt()(self.ask_prompt)
        self._mcp.prompt(name=self.CONSOLE)(self.console_prompt)
        self._mcp.completion()(self.complete)

    def _apps(self) -> Apps:
        """Приложение заказов и его инструменты."""
        apps = Apps()
        apps.tool(resource_uri=self.ORDERS)(self.orders)
        apps.tool(resource_uri=self.ORDERS)(self.orders_console)
        apps.tool(resource_uri=self.ORDERS, visibility=["app"])(self.refresh_orders)
        apps.tool(resource_uri=self.ORDERS, visibility=["model"])(self.archive_orders)
        html = Path(__file__).with_name("orders.html").read_text()
        apps.add_html_resource(self.ORDERS, html, csp=self.ORDERS_CSP)

        return apps

    def _extensions(
        self, apps: Apps, extended: bool, workspace: str, commands: bool
    ) -> tuple[Extension, ...]:
        """Расширения сервера: MCP Apps, с extended — объявленные не по
        моделям клиента, с workspace — файлы workspace под этим каталогом, с
        commands — расширение команд io.boba/commands."""
        extensions: tuple[Extension, ...] = (apps,)
        if extended:
            extensions = (apps, *self.EXTENSIONS)

        if workspace:
            files = {"path": "/files", "upload": "show", "workspace": workspace}
            extensions = (*extensions, DeclaredExtension("com.boba/files", files))

        if commands:
            declared = DeclaredExtension(self.COMMANDS, self.COMMANDS_SETTINGS)
            extensions = (*extensions, declared)

        return extensions

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

    def pid(self) -> int:
        """The process id of the server."""
        return os.getpid()

    def region(
        self,
        region: Annotated[str, Field(json_schema_extra={"x-mcp-header": "Region"})],
    ) -> str:
        """Echoes the region the client mirrors into the Mcp-Param-Region header."""
        return f"region {region}"

    def area(
        self,
        scope_id: Annotated[
            str, Field(default="", json_schema_extra={"x-boba-scope": True})
        ],
    ) -> str:
        """Echoes the scope parameter: the server marks it like boba-mcp does
        but declares no com.boba/scope extension, so the mark means nothing."""
        return f"area {scope_id!r}"

    def retitle(self, text: str) -> str:
        """Replaces the instructions server/discover gives."""
        self._mcp._lowlevel_server.instructions = text
        return f"instructions: {text}"

    def retire_modern(self, mode: str) -> str:
        """Stops speaking 2026-07-28 over HTTP: refuses its requests by mode."""
        switch = self._switch
        if switch is None:
            msg = "retire_modern works only over streamable HTTP"
            raise ToolError(msg)

        switch.mode = mode
        return f"modern requests are refused: {mode}"

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

    def console_prompt(self, status: str) -> str:
        """Opens the orders console with the orders of the given status."""
        return (
            f"Open the orders console for the orders with status {status}: call "
            f"the tool {self.CONSOLE} with status {status}."
        )

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

    def orders(self, ctx: Context, rows: list[OrderRow]) -> str:
        """Shows the given orders in a table the user can filter."""
        if client_supports_apps(ctx):
            return f"{len(rows)} orders are shown by their app"

        lines = [f"{len(rows)} orders:"]
        for row in rows:
            lines.append(f"#{row.id} {row.customer} {row.status} {row.total:.2f}")

        return "\n".join(lines)

    def orders_console(self, status: str) -> str:
        """Opens the orders console with the orders of the given status."""
        return f"the orders console is open for {status} orders"

    def refresh_orders(self) -> str:
        """Refreshes the orders; only their app calls it."""
        return "orders refreshed"

    def archive_orders(self) -> str:
        """Archives the orders; only the model calls it."""
        return "orders archived"

    def run(self, transport: str) -> None:
        if transport.startswith("stdio"):
            self._mcp.run("stdio")
            return

        app = self._mcp.streamable_http_app(host="127.0.0.1")
        self._switch = ModernSwitch(app, self._retired)
        uvicorn.run(
            self._switch, host="127.0.0.1", port=self._port, log_level="warning"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("transport")
    parser.add_argument("port", nargs="?", type=int, default=0)
    parser.add_argument("--ttl-ms", type=int, default=0)
    parser.add_argument("--retired", default="")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--commands", action="store_true")
    args = parser.parse_args()

    extended = args.transport == "stdio-extended"
    StandardServer(
        args.port,
        extended,
        args.ttl_ms,
        args.retired,
        args.workspace,
        args.commands,
    ).run(args.transport)
