"""Карточки и ответы MCP в контрактах toolkit.

Общие правила annotations, audience и представления содержимого держит
boba.mcp_models. Адаптер добавляет виды аргументов и схему результата к
карточке, а ответ превращает в семейство ToolResultBase.

Ошибки:
McpToolNameError — имя сервера или инструмента не годится для полного имени.
"""

from __future__ import annotations

from enum import StrEnum

import mcp_types as mt
from mcp_types.jsonrpc import ErrorData

from boba.mcp_models.protocol import ToolCards, ToolReplies
from boba.toolkit.calls import CallViews
from boba.toolkit.dag import ToolCard, ToolHints
from boba.toolkit.result import ErrorResult, MarkdownResult, ToolResultBase

__all__ = ["McpFailure", "McpResults", "McpToolStubs"]


class McpFailure(StrEnum):
    """Виды отказа вызова инструмента сервера MCP, которые собирает клиент."""

    TOOL_ERROR = "mcp_tool_error"
    TRANSPORT = "mcp_transport_error"


class McpToolStubs:
    """Адаптер общих карточек MCP к ToolCard toolkit.

    Клиент MCP получает виды аргументов через CallViews и схему результата;
    серверный адаптер использует обратное преобразование annotations.
    """

    def __init__(self) -> None:
        self._cards = ToolCards()
        self._views = CallViews()

    def card(self, server: str, tool: mt.Tool) -> ToolCard:
        """Карточка инструмента tool сервера server.

        Ошибки:
        McpToolNameError — имя сервера или инструмента не годится для
            полного имени.
        """
        card = self._cards.card(server, tool)

        return ToolCard(
            name=card.name,
            description=card.description,
            parameters=card.parameters,
            views=self._views.seen(tool.input_schema),
            output=tool.output_schema,
            hints=card.hints,
        )

    def hints_of(self, tool: mt.Tool) -> ToolHints:
        """Пометки по annotations сервера; без них — меняющий и разрушающий."""
        return self._cards.hints_of(tool)

    def annotations_of(self, hints: ToolHints) -> mt.ToolAnnotations:
        """annotations инструмента для клиента MCP по его пометкам."""
        return self._cards.annotations_of(hints)


class McpResults:
    """Адаптер ответа MCP к результату toolkit для клиента инструментов.

    ToolReplies формирует текст модели, адаптер выбирает MarkdownResult или
    ErrorResult. Запрос дополнительного ввода остаётся явной ошибкой.
    """

    def __init__(self) -> None:
        self._replies = ToolReplies()

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

        text = self._replies.result_text(reply, tool, call_id)
        if not reply.is_error:
            return MarkdownResult(text=text)

        return ErrorResult(message=text, error_kind=McpFailure.TOOL_ERROR)

    def error(self, tool: str, call_id: str, error: ErrorData) -> ErrorResult:
        """Отказ вызову call_id инструмента tool с ошибкой протокола MCP."""
        text = self._replies.error_text(tool, call_id, error)

        return ErrorResult(message=text, error_kind=McpFailure.TOOL_ERROR)

    def prompt(self, reply: mt.GetPromptResult) -> str:
        """Сообщения prompts/get как текст для модели по общим правилам MCP."""
        return self._replies.prompt(reply)
