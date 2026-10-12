"""Имена инструментов серверов MCP: полное имя и его разбор.

Инструмент сервера MCP виден модели, хуку и ленте под одним полным именем
`mcp__<server>__<tool>`: по нему однозначно находится и сервер, и имя
инструмента на нём, а одноимённые инструменты разных серверов не
сталкиваются. Имя сервера не пустое, не содержит `__` и не кончается на
`_` — иначе полное имя не разбирается обратно; в имени инструмента `__`
допустим.

Ошибки:
McpToolNameError — имя сервера или инструмента не годится для полного имени.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["McpNamePart", "McpToolName", "McpToolNameError", "McpToolNames"]


class McpToolNameError(ValueError):
    """Имя сервера или инструмента не годится для полного имени."""


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
