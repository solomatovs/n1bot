"""Инструменты серверов MCP для модели: полное имя, карточка и текст итога.

Инструмент сервера MCP виден модели, хуку и ленте под одним полным именем
`mcp__<server>__<tool>`: по нему однозначно находится и сервер, и имя
инструмента на нём, а одноимённые инструменты разных серверов не
сталкиваются. Имя сервера не пустое, не содержит `__` и не кончается на
`_` — иначе полное имя не разбирается обратно; в имени инструмента `__`
допустим. Карточку для модели строят из Tool списка tools/list, текст для
модели — из ответа tools/call по блокам, адресованным модели.

Ошибки:
McpToolNameError — имя сервера или инструмента не годится для полного имени.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import mcp_types as mt
from mcp_types.jsonrpc import ErrorData

from boba.toolkit.calls import CallViews
from boba.toolkit.dag import ToolCard, ToolHints
from boba.toolkit.result import ErrorResult, JsonBlock, MarkdownResult, ToolResultBase

__all__ = [
    "McpFailure",
    "McpNamePart",
    "McpResults",
    "McpToolName",
    "McpToolNameError",
    "McpToolNames",
    "McpToolStubs",
]


class McpToolNameError(ValueError):
    """Имя сервера или инструмента не годится для полного имени."""


class McpFailure(StrEnum):
    """Виды отказа вызова инструмента сервера MCP, которые собирает клиент."""

    TOOL_ERROR = "mcp_tool_error"
    TRANSPORT = "mcp_transport_error"


class McpNamePart(StrEnum):
    """Части формы полного имени `mcp__<server>__<tool>`."""

    PREFIX = "mcp__"
    SEPARATOR = "__"
    EDGE = "_"


@dataclass(frozen=True)
class McpToolName:
    """Инструмент сервера MCP: имя сервера и имя инструмента на нём."""

    server: str
    tool: str


class McpToolNames:
    """Единая точка работы с именами инструментов серверов MCP.

    Собирает полное имя инструмента по серверу и разбирает его обратно на
    сервер и имя инструмента на нём; имя, которое не начинается с приставки
    формы, — не инструмент сервера MCP (собственный инструмент агента).
    Объект держат в поле все, кому нужны такие имена: клиент MCP называет
    им карточки, агент ищет владельца вызова и строит событие хука, чат
    собирает матчер хука и подпись шага ленты.
    """

    def checked_server(self, server: str) -> str:
        """Имя сервера, годное для полного имени.

        Ошибки:
        McpToolNameError — имя пустое, содержит `__` или кончается на `_`.
        """
        if not server:
            raise McpToolNameError(self._refusal(server, "it is empty"))

        if McpNamePart.SEPARATOR in server:
            reason = f"it contains {McpNamePart.SEPARATOR.value!r}"
            raise McpToolNameError(self._refusal(server, reason))

        if server.endswith(McpNamePart.EDGE):
            reason = f"it ends with {McpNamePart.EDGE.value!r}"
            raise McpToolNameError(self._refusal(server, reason))

        return server

    def server_prefix(self, server: str) -> str:
        """Литеральное начало полных имён всех инструментов сервера.

        Ошибки:
        McpToolNameError — имя сервера не годится для полного имени.
        """
        checked = self.checked_server(server)

        return f"{McpNamePart.PREFIX.value}{checked}{McpNamePart.SEPARATOR.value}"

    def full(self, server: str, tool: str) -> str:
        """Полное имя инструмента tool сервера server.

        Ошибки:
        McpToolNameError — имя сервера не годится либо имя инструмента пустое.
        """
        prefix = self.server_prefix(server)
        if not tool:
            msg = (
                f"MCP server {server!r} names a tool with an empty name: a full "
                f"tool name {prefix}<tool> expects a non-empty tool name."
            )
            raise McpToolNameError(msg)

        return f"{prefix}{tool}"

    def parse(self, full: str) -> McpToolName | None:
        """Сервер и имя инструмента по полному имени; None — имя не
        инструмента сервера MCP."""
        if not full.startswith(McpNamePart.PREFIX):
            return None

        rest = full.removeprefix(McpNamePart.PREFIX)
        server, separator, tool = rest.partition(McpNamePart.SEPARATOR)
        if not separator:
            return None

        if not server:
            return None

        if not tool:
            return None

        return McpToolName(server=server, tool=tool)

    def in_server(self, server: str, name: str) -> str:
        """Полное имя инструмента сервера server по имени из узла вызова
        workflow этого сервера: модель называет узлу инструмент либо полным
        именем из своего списка, либо именем на сервере.

        Ошибки:
        McpToolNameError — имя сервера не годится либо имя инструмента пустое.
        """
        parsed = self.parse(name)
        if parsed is None:
            return self.full(server, name)

        if parsed.server != server:
            return self.full(server, name)

        return name

    def tool_of(self, name: str) -> str:
        """Имя инструмента без сервера: у инструмента сервера MCP — его имя
        на сервере, у собственного инструмента агента — само имя."""
        parsed = self.parse(name)
        if parsed is None:
            return name

        return parsed.tool

    @staticmethod
    def _refusal(server: str, reason: str) -> str:
        return (
            f"MCP server name {server!r} cannot be part of the tool names "
            f"{McpNamePart.PREFIX.value}<server>{McpNamePart.SEPARATOR.value}<tool>: "
            f"{reason}. Expected a non-empty name without "
            f"{McpNamePart.SEPARATOR.value!r} that does not end with "
            f"{McpNamePart.EDGE.value!r}; rename the server."
        )


class McpToolStubs:
    """Карточки инструментов модели по списку инструментов MCP-сервера.

    Карточка несёт полное имя инструмента (McpToolNames: сервер и имя на
    нём), описание, схему аргументов и схему итога сервера как есть, вид
    аргументов, выведенный из схемы (CallViews), и пометки по annotations
    сервера.
    Пометки переводятся в обе стороны: клиент MCP читает их из Tool сервера
    (hints_of), сервер boba (McpTool адаптера fastmcp) отдаёт их клиенту
    annotations (annotations_of).
    """

    def __init__(self) -> None:
        self._names = McpToolNames()
        self._views = CallViews()

    def card(self, server: str, tool: mt.Tool) -> ToolCard:
        """Карточка инструмента tool сервера server.

        Ошибки:
        McpToolNameError — имя сервера или инструмента не годится для
            полного имени.
        """
        description = tool.description
        if not description:
            description = tool.name

        return ToolCard(
            name=self._names.full(server, tool.name),
            description=description,
            parameters=dict(tool.input_schema),
            views=self._views.seen(tool.input_schema),
            output=tool.output_schema,
            hints=self.hints_of(tool),
        )

    def hints_of(self, tool: mt.Tool) -> ToolHints:
        """Пометки по annotations сервера; без них — меняющий и разрушающий."""
        annotations = tool.annotations
        if annotations is None:
            return ToolHints()

        read_only = False
        if annotations.read_only_hint is not None:
            read_only = annotations.read_only_hint

        destructive = True
        if annotations.destructive_hint is not None:
            destructive = annotations.destructive_hint

        return ToolHints(read_only=read_only, destructive=destructive)

    def annotations_of(self, hints: ToolHints) -> mt.ToolAnnotations:
        """annotations инструмента для клиента MCP по его пометкам."""
        return mt.ToolAnnotations(
            read_only_hint=hints.read_only,
            destructive_hint=hints.destructive,
        )


class McpResults:
    """Текст для модели из ответа сервера MCP на tools/call.

    Модель получает блоки, адресованные ей (без audience либо с assistant в
    ней): текстовый блок — своим текстом, любой другой — пометкой
    `[<вид> <имя или uri> <тип>]`; ответ без такого текста, но со
    structuredContent — его JSON. isError, просьба сервера о вводе
    (input_required) и ошибка самого MCP (error вместо result) дают модели
    ошибку. Зовут его клиенты MCP, которые
    кладут итог вызова в историю модели; итог вызова они собирают сами.
    """

    def result(
        self,
        tool: str,
        call_id: str,
        reply: mt.CallToolResult | mt.InputRequiredResult,
    ) -> ToolResultBase:
        """Результат вызова call_id инструмента tool по ответу сервера reply."""
        if isinstance(reply, mt.InputRequiredResult):
            message = (
                f"The MCP server asked for more input (input_required) before "
                f"finishing the call {call_id!r} of tool {tool!r}, and this "
                "client cannot answer such requests; call the tool with "
                "arguments that need no follow-up input."
            )
            return ErrorResult(message=message, error_kind=McpFailure.TOOL_ERROR)

        text = self._text(reply)
        if not reply.is_error:
            return MarkdownResult(text=text)

        if not text:
            text = (
                f"The MCP server reported an error for the call {call_id!r} of "
                f"tool {tool!r} without any text; check the server log for the "
                "cause."
            )

        return ErrorResult(message=text, error_kind=McpFailure.TOOL_ERROR)

    def error(self, tool: str, call_id: str, error: ErrorData) -> ErrorResult:
        """Результат вызова call_id инструмента tool, на который сервер
        ответил ошибкой MCP: код, текст и data, если она есть."""
        message = (
            f"The MCP server answered the call {call_id!r} of tool {tool!r} with "
            f"error {error.code}: {error.message}"
        )
        if error.data is not None:
            message = f"{message}\n{JsonBlock.pretty(error.data)}"

        return ErrorResult(message=message, error_kind=McpFailure.TOOL_ERROR)

    def _text(self, reply: mt.CallToolResult) -> str:
        addressed = filter(self._for_model, reply.content)
        parts = map(self._block_text, addressed)
        filled = filter(None, parts)
        text = "\n".join(filled)
        if text:
            return text

        if reply.structured_content is not None:
            return JsonBlock.pretty(reply.structured_content)

        return ""

    def _for_model(self, block: mt.ContentBlock) -> bool:
        """Блок адресован модели: audience не задана либо называет assistant."""
        annotations = block.annotations
        if annotations is None:
            return True

        audience = annotations.audience
        if audience is None:
            return True

        return "assistant" in audience

    def _block_text(self, block: mt.ContentBlock) -> str:
        if isinstance(block, mt.TextContent):
            return block.text

        if isinstance(block, mt.ResourceLink):
            words = [block.type, block.name]
            mime = block.mime_type
        elif isinstance(block, mt.EmbeddedResource):
            words = [block.type, block.resource.uri]
            mime = block.resource.mime_type
        else:
            words = [block.type]
            mime = block.mime_type

        if mime:
            words.append(mime)

        marked = " ".join(words)

        return f"[{marked}]"
