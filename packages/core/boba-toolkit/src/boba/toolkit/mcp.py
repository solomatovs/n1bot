"""Инструменты серверов MCP для модели: карточка и текст итога.

Инструмент сервера MCP виден модели под полным именем `mcp__<server>__<tool>`
(форма — McpToolNames ядра). Карточку для модели строят из Tool списка
tools/list, текст для модели — из ответа tools/call по блокам, адресованным
модели.

Ошибки:
McpToolNameError — имя сервера или инструмента не годится для полного имени.
"""

from __future__ import annotations

from enum import StrEnum

import mcp_types as mt
from mcp_types.jsonrpc import ErrorData

from boba.core.mcp_names import McpToolNames
from boba.toolkit.calls import CallViews
from boba.toolkit.dag import ToolCard, ToolHints
from boba.toolkit.result import (
    BlockAudience,
    ErrorResult,
    JsonBlock,
    MarkdownResult,
    ToolResultBase,
)

__all__ = [
    "McpFailure",
    "McpResults",
    "McpToolStubs",
]


class McpFailure(StrEnum):
    """Виды отказа вызова инструмента сервера MCP, которые собирает клиент."""

    TOOL_ERROR = "mcp_tool_error"
    TRANSPORT = "mcp_transport_error"


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
    """Текст для модели из ответа сервера MCP на tools/call и из сообщений
    промпта на prompts/get.

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

    def prompt(self, reply: mt.GetPromptResult) -> str:
        """Текст для модели из сообщений промпта сервера (prompts/get): блок
        каждого сообщения — тем же правилом, что блоки итога вызова;
        сообщение ассистента помечено ролью."""
        parts: list[str] = []
        if reply.description:
            parts.append(reply.description)

        for message in reply.messages:
            text = self._block_text(message.content)
            if message.role == "assistant":
                text = f"[assistant] {text}"

            parts.append(text)

        return "\n\n".join(parts)

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

        return BlockAudience.MODEL.reads(annotations.audience)

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
