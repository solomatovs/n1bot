"""Инструменты серверов MCP глазами агента: полное имя, карточка, ответ вызова.

Агент говорит с серверами инструментов только по MCP: описание инструмента
приходит `Tool` списка tools/list, пометки — его стандартными annotations
(readOnlyHint, destructiveHint), итог вызова — `CallToolResult` ответа
tools/call. Инструмент сервера виден модели, разрешениям и ленте под одним
полным именем `mcp__<server>__<tool>`: по нему однозначно находится и
сервер, и имя инструмента на нём, а одноимённые инструменты разных
серверов не сталкиваются. Имя сервера не пустое, не содержит `__` и не
кончается на `_` — иначе полное имя не разбирается обратно; в имени
инструмента `__` допустим. Текст для модели агент собирает сам из блоков
ответа, адресованных ей; ответ как пришёл ложится в историю записью
`toolUseResult` и уходит клиенту `rawOutput`.

Ошибки:
McpToolNameError — имя сервера или инструмента не годится для полного имени.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import mcp_types as mt
from mcp_types.jsonrpc import ErrorData

__all__ = [
    "Audience",
    "McpNamePart",
    "McpToolName",
    "McpToolNameError",
    "McpToolNames",
    "ToolCard",
    "ToolCards",
    "ToolHints",
    "ToolReplies",
    "ToolReply",
]


class McpToolNameError(ValueError):
    """Имя сервера или инструмента не годится для полного имени."""


class McpNamePart(StrEnum):
    """Части формы полного имени `mcp__<server>__<tool>`."""

    PREFIX = "mcp__"
    SEPARATOR = "__"
    EDGE = "_"
    ALL = "*"


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
    Объект держат в поле все, кому нужны такие имена: вход процесса
    называет им карточки и команды сессии, вложения контекста группируют
    по нему снятые инструменты.
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

    def full(self, server: str, tool: str) -> str:
        """Полное имя инструмента tool сервера server.

        Ошибки:
        McpToolNameError — имя сервера не годится либо имя инструмента пустое.
        """
        checked = self.checked_server(server)
        prefix = f"{McpNamePart.PREFIX.value}{checked}{McpNamePart.SEPARATOR.value}"
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

    def group(self, name: str) -> str:
        """Группа инструментов сервера `mcp__<server>__*`, куда входит name;
        имя не инструмента сервера MCP — само имя."""
        parsed = self.parse(name)
        if parsed is None:
            return name

        prefix = McpNamePart.PREFIX.value
        separator = McpNamePart.SEPARATOR.value

        return f"{prefix}{parsed.server}{separator}{McpNamePart.ALL.value}"

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


class Audience(StrEnum):
    """Кому адресован блок или ресурс: роли annotations.audience стандарта
    MCP; без audience блок видят обе стороны."""

    MODEL = "assistant"
    USER = "user"

    def reads(self, audience: Sequence[str] | None) -> bool:
        if audience is None:
            return True

        return self.value in audience


@dataclass(frozen=True)
class ToolHints:
    """Пометки инструмента по стандартным annotations MCP: читает ли он
    только (readOnlyHint) и может ли разрушить данные (destructiveHint).

    По ним разрешения решают, спрашивать ли подтверждение перед вызовом.
    Инструмент без пометок считается меняющим и разрушающим — так же
    трактует отсутствие annotations стандарт MCP.
    """

    read_only: bool = False
    destructive: bool = True


@dataclass(frozen=True)
class ToolCard:
    """Карточка инструмента сервера для модели: полное имя, описание,
    JSON-схема аргументов и пометки."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    hints: ToolHints


class ToolCards:
    """Карточки инструментов модели по `Tool` списка tools/list сервера:
    полное имя (McpToolNames), описание, схема аргументов как есть и пометки
    по annotations. Держит вход процесса на каждый сервер сессии."""

    def __init__(self) -> None:
        self._names = McpToolNames()

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


@dataclass(frozen=True)
class ToolReply:
    """Ответ на вызов инструмента: результат tools/call и он же как пришёл.

    raw — JSON ответа без разбора: его хранит история записью
    `toolUseResult` и несёт клиенту `rawOutput`; у ответа, который собрал
    сам агент (отказ, собственный инструмент), это дамп result.
    """

    result: mt.CallToolResult
    raw: Any


class ToolReplies:
    """Ответы на вызовы инструментов в форме MCP и текст для модели по ним.

    Создают его исполнитель вызовов (ToolTexts), собственные инструменты
    агента и порт сервера: ответ сервера принимается как пришёл (received),
    отказ и итог собственного инструмента собираются результатом tools/call
    (made, refused). Модель получает блоки, адресованные ей (без audience
    либо с assistant в ней): текстовый блок — своим текстом, любой другой —
    пометкой `[<вид> <имя или uri> <тип>]`; ответ без такого текста, но со
    structuredContent — его JSON; isError без текста — ошибка с подсказкой.
    """

    def received(self, raw: Any, result: mt.CallToolResult) -> ToolReply:
        """Ответ сервера: raw — как пришёл, result — он же разобранный."""
        return ToolReply(result=result, raw=raw)

    def made(
        self, text: str, structured: Mapping[str, Any] | None, is_error: bool
    ) -> ToolReply:
        """Ответ, собранный агентом: текст одним блоком, structuredContent
        и признак ошибки."""
        result = mt.CallToolResult(
            content=[mt.TextContent(text=text)],
            structured_content=structured,
            is_error=is_error,
        )

        return ToolReply(result=result, raw=self.dumped(result))

    def refused(self, message: str) -> ToolReply:
        """Отказ вызову с причиной: isError и текст."""
        return self.made(message, None, True)

    def error(self, tool: str, call_id: str, error: ErrorData) -> ToolReply:
        """Отказ вызову call_id инструмента tool, на который сервер ответил
        ошибкой MCP: код, текст и data, если она есть."""
        message = (
            f"The MCP server answered the call {call_id!r} of tool {tool!r} with "
            f"error {error.code}: {error.message}"
        )
        if error.data is not None:
            message = f"{message}\n{self.pretty(error.data)}"

        return self.refused(message)

    def text(self, reply: ToolReply, tool: str, call_id: str) -> str:
        """Текст для модели по ответу reply на вызов call_id инструмента tool."""
        result = reply.result
        text = self._text(result)
        if text:
            return text

        if not result.is_error:
            return ""

        return (
            f"The MCP server reported an error for the call {call_id!r} of "
            f"tool {tool!r} without any text; check the server log for the cause."
        )

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

    def dumped(self, result: mt.CallToolResult) -> dict[str, Any]:
        """Результат tools/call в JSON по ключам протокола, без null."""
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    @staticmethod
    def pretty(payload: Any) -> str:
        """Json-текст с отступами; чужие типы приводятся строкой."""
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    def _text(self, result: mt.CallToolResult) -> str:
        addressed = filter(self._for_model, result.content)
        parts = map(self._block_text, addressed)
        filled = filter(None, parts)
        text = "\n".join(filled)
        if text:
            return text

        if result.structured_content is not None:
            return self.pretty(result.structured_content)

        return ""

    def _for_model(self, block: mt.ContentBlock) -> bool:
        """Блок адресован модели: audience не задана либо называет assistant."""
        annotations = block.annotations
        if annotations is None:
            return True

        return Audience.MODEL.reads(annotations.audience)

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
